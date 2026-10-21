"""NY Midnight Open (course setups, step 11): time anchors with DST, prices, ATR, Judas flags, causality, verdict."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from tradedesk.analytics.ny_midnight_open import (
    BAR_MS,
    HOUR_MS,
    causal_atr,
    coverage,
    daily_ny_bars,
    day_prices,
    extremes,
    judas,
    ny_date,
    ny_ms,
    nymo_verdict,
    paired_summary,
    price_at,
    thesis_move,
    utc_midnight_ms,
)
from tradedesk.analytics.session_stop_hunt import Bars


def mk(t: np.ndarray, c: np.ndarray, spread: float = 0.5) -> Bars:
    o = np.concatenate([[c[0]], c[:-1]])
    return Bars(t.astype(np.int64), o, np.maximum(o, c) + spread, np.minimum(o, c) - spread, c)


def walk(first: date, days: int, seed: int = 1) -> Bars:
    t0 = utc_midnight_ms(first)
    n = days * 288
    t = t0 + np.arange(n, dtype=np.int64) * BAR_MS
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 0.1, n))
    return mk(t, c)


def test_ny_midnight_follows_dst() -> None:
    h = lambda d: (ny_ms(d, 0) - utc_midnight_ms(d)) // HOUR_MS  # noqa: E731
    assert h(date(2024, 3, 8)) == 5  # EST
    assert h(date(2024, 3, 11)) == 4  # EDT (switch on Sunday 2024-03-10)
    assert h(date(2024, 11, 1)) == 4
    assert h(date(2024, 11, 4)) == 5
    assert ny_ms(date(2024, 7, 1), 9, 30) - ny_ms(date(2024, 7, 1), 0) == 9 * HOUR_MS + 30 * 60_000
    assert ny_date(ny_ms(date(2024, 7, 1), 23, 59)) == date(2024, 7, 1)


def test_price_at_open_or_recent_close() -> None:
    t = np.array([0, BAR_MS, 2 * BAR_MS, 10 * BAR_MS], dtype=np.int64)
    b = Bars(t, np.array([1.0, 2, 3, 4]), np.array([1.5, 2.5, 3.5, 4.5]), np.array([0.5, 1.5, 2.5, 3.5]), np.array([1.1, 2.1, 3.1, 4.1]))
    assert price_at(b, BAR_MS) == 2.0  # the open of the bar opening at T
    assert price_at(b, 4 * BAR_MS) == pytest.approx(3.1)  # last close 10 min earlier
    assert math.isnan(price_at(b, 9 * BAR_MS))  # last bar 35 min earlier: too old
    assert coverage(b, 0, 4 * BAR_MS) == pytest.approx(3 / 4)
    assert extremes(b, 0, 2 * BAR_MS) == (2.5, 0.5)
    assert all(math.isnan(x) for x in extremes(b, 3 * BAR_MS, 5 * BAR_MS))


def test_thesis_move_signs() -> None:
    s, y, r = thesis_move(ref=100, p_dec=98, p_exit=99, atr=2)  # below the reference: long, +1 move
    assert (s, y, r) == (1, 0.5, pytest.approx(1 / 98))
    s, y, r = thesis_move(ref=100, p_dec=102, p_exit=101, atr=2)  # above: short, price falls -> positive
    assert (s, y) == (-1, 0.5) and r == pytest.approx(1 / 102)
    s, y, _ = thesis_move(ref=100, p_dec=102, p_exit=104, atr=2)
    assert y == -1.0
    assert thesis_move(100, 100, 105, 2)[:2] == (0, 0.0)
    assert math.isnan(thesis_move(float("nan"), 100, 105, 2)[1])


def test_causal_atr_uses_only_previous_days() -> None:
    days = [date(2024, 1, 1) + timedelta(days=k) for k in range(5)]
    daily = [(d, 10.0 + k, 8.0, 9.0, 1.0) for k, d in enumerate(days)]  # ranges 2, 3, 4, 5, 6; prev close 9
    atr = causal_atr(daily, n=2)
    assert math.isnan(atr[days[0]]) and math.isnan(atr[days[1]])
    assert atr[days[2]] == pytest.approx((2 + 3) / 2)  # true range of day 1: max(3, |11-9|, |8-9|) = 3
    assert atr[days[4]] == pytest.approx((4 + 5) / 2)
    # an invalid (low coverage) day is skipped
    daily[2] = (days[2], 50.0, 1.0, 9.0, 0.1)
    atr2 = causal_atr(daily, n=2)
    assert atr2[days[3]] == pytest.approx((2 + 3) / 2)
    assert atr2[days[4]] == pytest.approx((3 + 5) / 2)


def test_daily_ny_bars_window_is_new_york_day() -> None:
    d = date(2024, 7, 2)
    b = walk(date(2024, 7, 1), 3)
    row = daily_ny_bars(b, [d])[0]
    i0, i1 = np.searchsorted(b.t, ny_ms(d, 0)), np.searchsorted(b.t, ny_ms(d + timedelta(days=1), 0))
    assert row[1] == pytest.approx(b.h[i0:i1].max()) and row[3] == pytest.approx(b.c[i1 - 1])
    assert row[4] == pytest.approx(1.0)


def test_day_prices_and_coverage() -> None:
    b = walk(date(2024, 7, 1), 4)
    d = date(2024, 7, 2)
    px, why = day_prices(b, d)
    assert why == "ok" and px is not None
    assert px["d00"] == b.o[np.searchsorted(b.t, ny_ms(d, 0))]
    assert px["uo"] == b.o[np.searchsorted(b.t, utc_midnight_ms(d))]
    assert px["p19"] == b.o[np.searchsorted(b.t, ny_ms(d - timedelta(days=1), 19))]
    # drop half of the day's bars -> coverage rejection
    keep = ~((b.t >= ny_ms(d, 2)) & (b.t < ny_ms(d, 12)))
    thin = Bars(b.t[keep], b.o[keep], b.h[keep], b.low[keep], b.c[keep])
    assert day_prices(thin, d)[1] == "coverage"


def test_judas_flags() -> None:
    a = ny_ms(date(2024, 7, 2), 0)
    n = 16 * 12 + 12
    t = a + np.arange(n, dtype=np.int64) * BAR_MS
    c = np.full(n, 100.0)
    c[10:20] = 97.0  # dips 3 below the open inside the window
    c[150:] = 104.0  # closes the day above the open
    b = Bars(
        t,
        np.concatenate([[100.0], c[:-1]]),
        np.maximum(np.concatenate([[100.0], c[:-1]]), c),
        np.minimum(np.concatenate([[100.0], c[:-1]]), c),
        c,
    )
    flags, why = judas(b, a, atr=10.0, ks=(0.0, 0.25, 0.5))
    assert why == "ok" and flags == {0.0: 1, 0.25: 1, 0.5: 0}  # dip 3 = 0.3 ATR
    # bearish day with no excursion above the open -> no Judas
    c2 = c.copy()
    c2[150:] = 96.0
    b2 = mk(t, c2, spread=0.0)
    flags2, _ = judas(b2, a, atr=10.0)
    assert flags2 == {0.0: 0}
    assert judas(b, a, atr=float("nan"))[1] == "atr"


def test_no_lookahead_day_record_and_judas() -> None:
    b = walk(date(2024, 6, 1), 40, seed=7)
    d = date(2024, 7, 2)
    days = [date(2024, 6, 1) + timedelta(days=k) for k in range(40)]
    atr = causal_atr(daily_ny_bars(b, days))
    px, _ = day_prices(b, d)
    j, _ = judas(b, ny_ms(d, 0), atr[d], ks=(0.0, 0.1))
    cut = ny_ms(d, 16)
    late = b.t > cut  # everything after 16:00 NY of day d changes
    rng = np.random.default_rng(9)
    c2 = b.c.copy()
    c2[late] = c2[late] * rng.uniform(0.5, 1.5, int(late.sum()))
    b2 = mk(b.t, c2)
    b2 = Bars(b.t, np.where(late, b2.o, b.o), np.where(late, b2.h, b.h), np.where(late, b2.low, b.low), c2)
    atr2 = causal_atr(daily_ny_bars(b2, days))
    assert atr2[d] == pytest.approx(atr[d])
    assert day_prices(b2, d)[0] == px
    assert judas(b2, ny_ms(d, 0), atr2[d], ks=(0.0, 0.1))[0] == j


def test_paired_summary_matches_mean_and_ci() -> None:
    rng = np.random.default_rng(2)
    v = rng.normal(0.1, 1.0, 2000)
    s = paired_summary(v.tolist(), list(range(2000)), n_boot=2000)
    assert s["mean"] == pytest.approx(float(v.mean()))
    assert s["ci"][0] < s["mean"] < s["ci"][1]
    assert s["se"] == pytest.approx(1 / math.sqrt(2000), rel=0.15)
    assert s["p"] < 0.05 and s["p2"] < 0.1
    s0 = paired_summary([1.0, float("nan")], ["a", "b"])
    assert s0["n"] == 1 and s0["ci"] is None


def test_nymo_verdict_rules() -> None:
    base = dict(
        n_days=1000,
        mde=0.03,
        diff=0.08,
        ci_day=(0.02, 0.14),
        ci_week=(0.01, 0.15),
        p_holm=0.01,
        oos_diff=0.05,
        oos_p=0.02,
        trade_ci_is=(0.0001, 0.003),
        trade_oos_mean=0.001,
    )
    assert nymo_verdict(**base) == "confirmed"  # type: ignore[arg-type]
    assert nymo_verdict(**{**base, "n_days": 400}) == "inconclusive"  # type: ignore[arg-type]
    assert nymo_verdict(**{**base, "mde": 0.06}) == "inconclusive"  # type: ignore[arg-type]
    assert nymo_verdict(**{**base, "diff": 0.04}) == "rejected"  # type: ignore[arg-type]
    assert nymo_verdict(**{**base, "ci_day": (-0.01, 0.1)}) == "rejected"  # type: ignore[arg-type]
    assert nymo_verdict(**{**base, "p_holm": 0.2}) == "inconclusive"  # type: ignore[arg-type]
    assert nymo_verdict(**{**base, "trade_ci_is": (-0.001, 0.002)}) == "inconclusive"  # type: ignore[arg-type]
    assert nymo_verdict(**{**base, "oos_diff": -0.01}) == "inconclusive"  # type: ignore[arg-type]
