"""The six classic-rule specs of IDEA-20261017-setup-hunt stage 5: registered parameters, indicators on hand-computed
inputs, signals on constructed series, and causality (no look-ahead) of every spec."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, segment_pos
from tradedesk.analytics.strategy_sieve import causality_violations, stamp_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("connors_rsi2", "bollinger_reversion", "tsmom_12m", "sma_cross_50_200", "williams_vol_breakout", "keltner_breakout")


def load(name: str):
    return sieve_specs.load(name)


def daily(c, h=None, lo=None, t0: int = D0) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) if h is None else np.asarray(h, dtype=float)
    lo = np.minimum(o, c) if lo is None else np.asarray(lo, dtype=float)
    return Bars(t0 + np.arange(c.size, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)


def random_walk(n: int, seed: int, bar_ms: int, vol: float) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def test_registered_parameters():
    # the stage-5 pre-registration of the card, no variants
    assert load("connors_rsi2").PARAMS == {
        "rsi_n": 2,
        "trend_n": 200,
        "exit_n": 5,
        "lo": 5.0,
        "hi": 95.0,
        "hold_bars": 10,
        "vol_n": 60,
        "stop_sigma": 2.0,
    }
    assert load("bollinger_reversion").PARAMS == {"n": 20, "k": 2.0, "hold_bars": 20, "vol_n": 60, "stop_sigma": 2.0}
    assert load("tsmom_12m").PARAMS == {"lookback_days": 365, "vol_n": 60, "stop_sigma": 2.0, "max_lag_days": 3}
    assert load("sma_cross_50_200").PARAMS == {"fast": 50, "slow": 200, "hold_bars": 50, "vol_n": 60, "stop_sigma": 2.0}
    assert load("williams_vol_breakout").PARAMS == {"k": 0.5}
    assert load("keltner_breakout").PARAMS == {"ema_n": 20, "atr_n": 10, "k": 2.0, "hold_bars": 20}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
        assert spec.TIMEFRAMES == ((1,) if name == "williams_vol_breakout" else (24,))


# ---------------------------------------------------------------- indicators


def test_rsi_wilder_by_hand():
    spec = load("connors_rsi2")
    c = np.array([1.0, 2.0, 3.0, 2.0, 2.0])
    r = spec.rsi_wilder(c, 2, segment_pos(D0 + np.arange(5) * DAY_MS))
    # changes +1 +1 -1 0: seed ag = 1, al = 0 -> 100; then ag = 0.5, al = 0.5 -> 50; then 0.25 / 0.25 -> 50
    assert math.isnan(r[0]) and math.isnan(r[1])
    assert r[2] == 100.0 and r[3] == pytest.approx(50.0) and r[4] == pytest.approx(50.0)


def test_sigma_and_sma_by_hand():
    spec = load("connors_rsi2")
    c = 100 * np.exp(np.array([0.0, 0.01, -0.01, 0.02, 0.0]))
    pos = segment_pos(D0 + np.arange(5) * DAY_MS)
    sd = spec.sigma_d(c, 3, pos)
    assert np.isnan(sd[:3]).all()
    # log returns 0.01, -0.02, 0.03, -0.02
    assert sd[3] == pytest.approx(np.std([0.01, -0.02, 0.03], ddof=1))
    assert sd[4] == pytest.approx(np.std([-0.02, 0.03, -0.02], ddof=1))
    m = spec.sma(c, 2, pos)
    assert math.isnan(m[0]) and m[1:] == pytest.approx((c[1:] + c[:-1]) / 2)


def test_bollinger_bands_by_hand():
    spec = load("bollinger_reversion")
    c = np.array([1.0, 2.0, 3.0, 4.0])
    mid, lower, upper = spec.bands(c, 3, 2.0, segment_pos(D0 + np.arange(4) * DAY_MS))
    sd = math.sqrt(2 / 3)  # population sd of (1, 2, 3)
    assert np.isnan(mid[:2]).all()
    assert mid[2] == pytest.approx(2.0) and lower[2] == pytest.approx(2 - 2 * sd) and upper[3] == pytest.approx(3 + 2 * sd)


def test_tsmom_months():
    spec = load("tsmom_12m")
    days = (np.array([D0, D0 + 30 * DAY_MS, D0 + 31 * DAY_MS, D0 + 59 * DAY_MS, D0 + 60 * DAY_MS]) // DAY_MS).astype(np.int64)
    # 2020-01-01, 01-31, 02-01, 02-29 (leap), 03-01
    m = spec.month_of_day(days)
    assert list(m - m[0]) == [0, 0, 1, 1, 2]
    assert list(spec.month_start_day(m)) == [days[0], days[0], days[2], days[2], days[4]]


# ---------------------------------------------------------------- signals on constructed series


def test_connors_long_after_a_two_day_drop_in_an_uptrend():
    spec = load("connors_rsi2")
    up = 100 * 1.005 ** np.arange(260)
    c = np.concatenate([up, up[-1] * np.array([0.97, 0.97**2, 0.97**3, 0.97**3 * 1.01])])
    b = daily(c)
    sigs = spec.signals(b, **spec.PARAMS)
    # Wilder RSI(2) after a long rise: 14.2 after the first -3 % day, 5.3 after the second, below 5 after the third
    pos = segment_pos(b.t)
    r = spec.rsi_wilder(c, 2, pos)
    assert r[260] == pytest.approx(14.16, abs=0.01) and r[261] == pytest.approx(5.31, abs=0.01) and r[262] < 5
    assert [s.t_ms for s in sigs] == [b.close_time(262)]
    s = sigs[0]
    e = c[262]
    sd = float(np.std(np.diff(np.log(c))[262 - 60 : 262], ddof=1))
    assert s.side == "long" and s.entry == e and s.target == pytest.approx(np.mean(c[258:263]))
    assert s.stop == pytest.approx(e - 2 * math.sqrt(10) * sd * e) and s.hold_ms == 10 * DAY_MS
    assert s.valid() and stamp_violations(b, sigs) == []


def test_connors_short_mirror():
    spec = load("connors_rsi2")
    dn = 100 * 0.995 ** np.arange(260)
    c = np.concatenate([dn, dn[-1] * np.array([1.03, 1.03**2, 1.03**3])])
    b = daily(c)
    sigs = spec.signals(b, **spec.PARAMS)
    assert [(s.t_ms, s.side) for s in sigs] == [(b.close_time(262), "short")]
    assert sigs[0].target == pytest.approx(np.mean(c[258:263])) and sigs[0].valid()


def test_bollinger_signals_beyond_the_bands():
    spec = load("bollinger_reversion")
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, 100)))
    c[90] = c[89] * 0.9  # far below the lower band
    b = daily(c)
    sigs = spec.signals(b, **spec.PARAMS)
    mid, lower, upper = spec.bands(c, 20, 2.0, segment_pos(b.t))
    expect = [b.close_time(i) for i in range(60, 100) if c[i] < lower[i] or c[i] > upper[i]]
    assert [s.t_ms for s in sigs] == expect and b.close_time(90) in expect
    s90 = next(s for s in sigs if s.t_ms == b.close_time(90))
    assert s90.side == "long" and s90.target == pytest.approx(mid[90]) and s90.hold_ms == 20 * DAY_MS
    assert all(s.valid() for s in sigs)


def test_tsmom_month_ends_and_sign():
    spec = load("tsmom_12m")
    n = 800
    c = 100 * 1.001 ** np.arange(n) * (1 + 0.01 * np.sin(np.arange(n)))
    b = daily(c)
    sigs = spec.signals(b, **spec.PARAMS)
    # first month end with a bar 365 days back: the bar opening 2020-12-31 (closing 2021-01-01 00:00)
    assert sigs[0].t_ms == D0 + 366 * DAY_MS
    assert sigs[0].hold_ms == 31 * DAY_MS  # to 2021-02-01 00:00
    assert all(s.side == "long" for s in sigs)  # +0.1 % a day: the 12-month return is always positive
    assert len(sigs) == 15  # month-end closes 2021-01-01 .. 2022-03-01 inside 800 days (last close 2022-03-11)
    s = sigs[0]
    i = 365
    sd = float(np.std(np.diff(np.log(c))[i - 60 : i], ddof=1))
    assert s.stop == pytest.approx(s.entry * (1 - 2 * sd * math.sqrt(31)))
    short = spec.signals(daily(c[::-1].copy()), **spec.PARAMS)
    assert short and all(s.side == "short" and s.valid() for s in short)


def test_tsmom_needs_the_bar_a_year_back():
    spec = load("tsmom_12m")
    c = 100 * 1.001 ** np.arange(800)
    b = daily(c)
    t = b.t.copy()
    t[200:] += 10 * DAY_MS  # a 10-day hole: a new segment, no 365-day lookback until a year after it
    b2 = Bars(t, b.o, b.h, b.low, b.c, DAY_MS)
    sigs = spec.signals(b2, **spec.PARAMS)
    assert sigs and min(s.t_ms for s in sigs) >= int(t[200]) + 365 * DAY_MS


def test_sma_cross_both_sides():
    spec = load("sma_cross_50_200")
    c = np.concatenate([np.linspace(200, 100, 300), np.linspace(100, 200, 300), np.linspace(200, 100, 300)])
    c = c * (1 + 0.002 * np.sin(np.arange(c.size)))  # some volatility for the stop
    b = daily(c)
    sigs = spec.signals(b, **spec.PARAMS)
    f = np.array([np.mean(c[i - 49 : i + 1]) if i >= 49 else np.nan for i in range(c.size)])
    s = np.array([np.mean(c[i - 199 : i + 1]) if i >= 199 else np.nan for i in range(c.size)])
    gold = [i for i in range(200, c.size) if f[i] > s[i] and f[i - 1] <= s[i - 1]]
    death = [i for i in range(200, c.size) if f[i] < s[i] and f[i - 1] >= s[i - 1]]
    assert len(gold) == 1 and len(death) == 1
    assert [(x.t_ms, x.side) for x in sigs] == sorted([(b.close_time(gold[0]), "long"), (b.close_time(death[0]), "short")])
    assert all(x.valid() and x.target is None and x.hold_ms == 50 * DAY_MS for x in sigs)


def test_keltner_first_close_above_the_band():
    spec = load("keltner_breakout")
    n = 40
    c = np.full(n + 3, 100.0)
    h = np.full(n + 3, 101.0)
    lo = np.full(n + 3, 99.0)
    c[n], h[n], lo[n] = 110.0, 110.0, 100.0  # breakout bar
    c[n + 1], h[n + 1], lo[n + 1] = 111.0, 112.0, 109.0  # still above: no new signal
    b = daily(c, h, lo)
    sigs = spec.signals(b, **spec.PARAMS)
    # ATR10 = 2 on the flat bars; the breakout TR = 10 -> (2 * 9 + 10) / 10 = 2.8; EMA20 = 100 + 10 * 2 / 21
    m = 100 + 10 * 2 / 21
    assert m + 2 * 2.8 < 110
    assert [(s.t_ms, s.side) for s in sigs] == [(b.close_time(n), "long")]
    assert sigs[0].stop == pytest.approx(m) and sigs[0].target is None and sigs[0].hold_ms == 20 * DAY_MS
    mirror = spec.signals(daily(200 - c, 200 - lo, 200 - h), **spec.PARAMS)
    assert [(s.t_ms, s.side) for s in mirror] == [(b.close_time(n), "short")]
    assert mirror[0].stop == pytest.approx(200 - m)


@pytest.mark.parametrize(
    "name, vol",
    # daily vol well above the skip threshold 1 / (2 sqrt(H)): 15.8 % / 11.2 % / ~9 % / 7.1 %
    [("connors_rsi2", 0.25), ("bollinger_reversion", 0.18), ("tsmom_12m", 0.15), ("sma_cross_50_200", 0.12)],
)
def test_emergency_stop_at_or_beyond_100pct_skips_the_signal(name, vol):
    # Not in the stage-5 pre-registration (disclosed after the critic's review): when 2 sigma_d sqrt(H) >= 100 % of the
    # entry price the signal is skipped on both sides. Pin it so that a change of this behaviour does not pass unnoticed.
    spec = load(name)
    b = random_walk(900, seed=11, bar_ms=DAY_MS, vol=vol)
    params = dict(spec.PARAMS)
    assert spec.signals(b, **params) == []
    params["stop_sigma"] = 0.1  # the same bars with a narrow stop: the raw rule does fire
    raw = spec.signals(b, **params)
    assert raw and all(s.valid() and abs(s.entry - s.stop) < s.entry for s in raw)


def hourly_days(days: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n = 24 * days
    return np.full(n, 100.0), np.full(n, 101.0), np.full(n, 99.0), np.full(n, 100.0)


def test_williams_levels_and_first_breakout():
    spec = load("williams_vol_breakout")
    o, h, lo, c = hourly_days(3)
    # day 1 (hours 24..47): open 100, yesterday's range 2 -> levels 99 / 101; hour 29 closes 101.5, hour 31 at 98
    c[29], h[29] = 101.5, 101.6
    c[31], lo[31] = 98.0, 97.9
    b = Bars(D0 + np.arange(72, dtype=np.int64) * HOUR_MS, o, h, lo, c, HOUR_MS)
    sigs = spec.signals(b, **spec.PARAMS)
    assert len(sigs) == 1  # day 2: yesterday's range 101.6 - 97.9, the flat day never reaches its levels
    s = sigs[0]
    assert s.t_ms == b.close_time(29) and s.side == "long" and s.entry == 101.5 and s.stop == pytest.approx(99.0)
    assert s.t_ms + s.hold_ms == D0 + 2 * DAY_MS  # exit at the next day's 00:00 UTC open
    assert s.valid() and stamp_violations(b, sigs) == []


def test_williams_needs_a_complete_previous_day_and_the_midnight_open():
    spec = load("williams_vol_breakout")
    o, h, lo, c = hourly_days(3)
    c[29] = 101.5
    keep = np.ones(72, dtype=bool)
    keep[10] = False  # day 0 lacks an hour
    b = Bars((D0 + np.arange(72, dtype=np.int64) * HOUR_MS)[keep], o[keep], h[keep], lo[keep], c[keep], HOUR_MS)
    assert spec.signals(b, **spec.PARAMS) == []
    keep = np.ones(72, dtype=bool)
    keep[24] = False  # day 1 starts at 01:00
    b = Bars((D0 + np.arange(72, dtype=np.int64) * HOUR_MS)[keep], o[keep], h[keep], lo[keep], c[keep], HOUR_MS)
    assert spec.signals(b, **spec.PARAMS) == []
    # the last hour of a day (close at 24:00) cannot signal: the exit would be its own close
    o, h, lo, c = hourly_days(3)
    c[47] = 101.5
    b = Bars(D0 + np.arange(72, dtype=np.int64) * HOUR_MS, o, h, lo, c, HOUR_MS)
    assert spec.signals(b, **spec.PARAMS) == []


# ---------------------------------------------------------------- causality


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    if name == "williams_vol_breakout":
        b = random_walk(24 * 150, seed=11, bar_ms=HOUR_MS, vol=0.008)
    else:
        b = random_walk(1500, seed=5, bar_ms=DAY_MS, vol=0.035)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 3 and all(s.valid() for s in sigs)
    assert {s.side for s in sigs} == {"long", "short"}
    m = len(b)
    cut = m * 2 // 3
    assert causality_violations(spec.signals, b, params, [m // 2, cut, int(m * 0.97)], n_sample=40, n_last=10) == []
    # changing the bars after t does not change the signals decided by t
    edge = int(b.t[cut - 1]) + b.bar_ms
    k = 1.0 + np.linspace(0.0, -0.5, m - cut)
    changed = Bars(
        b.t,
        np.concatenate([b.o[:cut], b.o[cut:] * k]),
        np.concatenate([b.h[:cut], b.h[cut:] * k * 1.01]),
        np.concatenate([b.low[:cut], b.low[cut:] * k * 0.99]),
        np.concatenate([b.c[:cut], b.c[cut:] * k]),
        b.bar_ms,
    )
    key = lambda s: (s.t_ms, s.side, round(s.stop, 9), None if s.target is None else round(s.target, 9), s.hold_ms)  # noqa: E731
    before = sorted(key(s) for s in sigs if s.t_ms <= edge)
    after = sorted(key(s) for s in spec.signals(changed, **params) if s.t_ms <= edge)
    assert before and before == after
