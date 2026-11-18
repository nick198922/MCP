"""Trader_XO 12/25 EMA bands (analytics.ema_bands): periods, directions, state lookup, causality."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from tradedesk.analytics import ema_bands as eb

H = eb.HOUR_MS
D = eb.DAY_MS


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


def _hours(start: str, n: int) -> np.ndarray:
    return _ms(start) + H * np.arange(n, dtype=np.int64)


def test_period_closes_daily_drops_thin_days_and_takes_last_close():
    t = _hours("2024-01-01T00:00", 72)
    c = np.arange(72, dtype=float)
    keep = np.ones(72, dtype=bool)
    keep[24:28] = False  # day 2 has 20 of 24 hours (< 90 %)
    keep[50] = False  # day 3 has 23 of 24 hours (>= 90 %)
    start, cl = eb.period_closes(t[keep], c[keep], H, D)
    assert start.tolist() == [_ms("2024-01-01T00:00"), _ms("2024-01-03T00:00")]
    assert cl.tolist() == [23.0, 71.0]


def test_weekly_periods_start_on_monday():
    t = _hours("2024-01-01T00:00", 24 * 14)  # 2024-01-01 is a Monday
    start, cl = eb.period_closes(t, np.ones(t.size), H, eb.WEEK_MS, eb.WEEK_OFFSET_MS)
    assert start.tolist() == [_ms("2024-01-01T00:00"), _ms("2024-01-08T00:00")]
    assert all(datetime.fromtimestamp(s / 1000, UTC).weekday() == 0 for s in start)
    # a series starting on a Wednesday: the first (partial) week is dropped, the next starts on Monday 08.01
    t2 = _hours("2024-01-03T00:00", 24 * 12)
    s2, _ = eb.period_closes(t2, np.ones(t2.size), H, eb.WEEK_MS, eb.WEEK_OFFSET_MS)
    assert s2.tolist() == [_ms("2024-01-08T00:00")]


def test_band_direction_rising_falling_flat():
    n_days = 40
    t = _hours("2024-01-01T00:00", 24 * n_days)
    day = (t - t[0]) // D
    up = eb.daily_band(t, 100.0 + day.astype(float))
    assert up.dir[: eb.SLOW - 1].tolist() == [0] * (eb.SLOW - 1)  # EMA25 undefined before 25 closes
    assert set(up.dir[eb.SLOW - 1 :].tolist()) == {1}  # fast EMA lags less on a rising series
    down = eb.daily_band(t, 200.0 - day.astype(float))
    assert set(down.dir[eb.SLOW - 1 :].tolist()) == {-1}
    flat = eb.daily_band(t, np.full(t.size, 7.0))
    assert set(flat.dir.tolist()) == {0}
    assert up.end[0] == _ms("2024-01-02T00:00")  # a period's state is known at its close


def test_states_at_uses_last_closed_period_and_goes_stale():
    b = eb.Band(np.array([10 * D, 11 * D, 12 * D]), np.array([1, -1, 1]), D)
    t = np.array([10 * D - 1, 10 * D, 11 * D - 1, 11 * D + 5, 14 * D, 14 * D + 1])
    assert eb.states_at(b, t).tolist() == [0, 1, 1, -1, 1, 0]


def test_alignment():
    assert eb.alignment("long", 1, 1) == "with"
    assert eb.alignment("short", -1, -1) == "with"
    assert eb.alignment(-1, 1, 1) == "against"
    assert eb.alignment(1, 1, -1) == "mixed"
    assert eb.alignment("long", 0, 1) == "undef"
    assert eb.alignments(np.array(["long", "short"]), np.array([1, 1]), np.array([1, 0])).tolist() == ["with", "undef"]


def test_states_before_t_do_not_depend_on_bars_after_t():
    rng = np.random.default_rng(7)
    n = 24 * 400
    t = _hours("2023-01-02T00:00", n)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    cut = _ms("2023-11-15T13:00")  # mid-week, mid-day
    edges = [cut - 1, cut, _ms("2023-11-15T00:00"), _ms("2023-11-13T00:00")]  # the day and (Monday) week of cut
    q = np.sort(np.concatenate([rng.integers(t[0], cut + 1, 300), edges]))
    base_w = eb.states_at(eb.weekly_band(t, c), q)
    base_d = eb.states_at(eb.daily_band(t, c), q)
    c2 = c.copy()
    after = t >= cut
    c2[after] = c[after] * np.exp(rng.normal(0, 0.2, int(after.sum())))  # a different future
    t3 = t[t < cut + 30 * H]  # and a truncated one
    for tt, cc in ((t, c2), (t3, c[: t3.size])):
        assert np.array_equal(eb.states_at(eb.weekly_band(tt, cc), q), base_w)
        assert np.array_equal(eb.states_at(eb.daily_band(tt, cc), q), base_d)
    assert set(base_w.tolist()) <= {-1, 0, 1} and (base_w != 0).any() and (base_d != 0).any()


def test_strata_codes_combine_keys():
    side = np.array(["long", "short", "long", "short", "long"])
    year = np.array([2021, 2021, 2022, 2021, 2021])
    s = eb.strata_codes(side, year)
    assert s[0] == s[4] and s[1] == s[3] and len({s[0], s[1], s[2]}) == 3
    assert sorted(set(s.tolist())) == [0, 1, 2]


def test_stratified_delta_analytic():
    # stratum 0: base R = [1, 2, 3, 4], flagged [3, 4] -> 3.5 - 2.5 = +1.0, n 4
    # stratum 1: base R = [0, 0, 1, 1, 2, 2], flagged [0, 0] -> 0 - 1 = -1.0, n 6
    # stratum 2: one flagged trade only -> skipped
    r = np.array([1, 2, 3, 4, 0, 0, 1, 1, 2, 2, 5, 6, 7], dtype=float)
    f = np.array([0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 1, 0, 0], dtype=bool)
    s = np.array([0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 2, 2, 2])
    res = eb.stratified_delta(r, f, np.ones(r.size, dtype=bool), s, weight="base")
    assert abs(res["d"] - (4 * 1.0 + 6 * -1.0) / 10) < 1e-12
    # registered weights n_a,s (2 and 2): the mean over flagged trades of R minus its stratum mean
    assert abs(eb.stratified_delta(r, f, np.ones(r.size, dtype=bool), s)["d"] - (2 * 1.0 + 2 * -1.0) / 4) < 1e-12
    assert res["used"] == 2 and res["total"] == 3 and res["n_a"] == 4 and res["n"] == 10
    # a single stratum reduces to the pooled lift
    one = eb.stratified_delta(r[:4], f[:4], np.ones(4, dtype=bool), np.zeros(4, dtype=np.int64))
    assert abs(one["d"] - 1.0) < 1e-12
    # the a-priori SE of a single stratum with sd 1: sqrt(1/2 - 1/4)
    assert abs(eb.stratified_prior_se(f[:4], np.ones(4, dtype=bool), np.zeros(4, dtype=np.int64)) - 0.5) < 1e-12


def test_stratification_removes_a_lift_made_by_side_only():
    """R depends only on the side (shorts +0.2, longs -0.2); the feature is far more common among shorts and unrelated
    to R within a side: the pooled lift is positive, the stratified one ~0 and its placebo p is not small."""
    rng = np.random.default_rng(11)
    n = 8000
    short = rng.random(n) < 0.5
    year = rng.choice([2021, 2022, 2023], n)
    r = np.where(short, 0.2, -0.2) + rng.normal(0, 0.5, n)
    flag = rng.random(n) < np.where(short, 0.6, 0.2)
    base = np.ones(n, dtype=bool)
    week = rng.integers(0, 150, n)
    pooled = r[flag].mean() - r.mean()
    assert pooled > 0.05
    st = eb.strata_codes(np.where(short, "short", "long"), year)
    ci = eb.stratified_delta_ci(r, flag, base, st, week, n_boot=2000, seed=3)
    assert abs(ci["d"]) < 0.03 and ci["lo"] < 0 < ci["hi"] and ci["used"] == 6
    pl = eb.stratified_placebo(r, flag, base, st, n_rep=500, seed=5)
    p_pl = (1 + np.sum(pl >= ci["d"])) / (1 + pl.size)
    assert p_pl > 0.05
    assert pooled > np.quantile(pl, 0.999)  # the pooled lift would look "significant" against the stratified null


def test_stratified_ci_detects_a_lift_within_strata():
    rng = np.random.default_rng(13)
    n = 6000
    s = rng.integers(0, 4, n)
    flag = rng.random(n) < 0.3 + 0.1 * s
    r = 0.3 * flag + 0.1 * s + rng.normal(0, 0.5, n)  # +0.3 inside every stratum: lift = 0.3 * (1 - share_s)
    ci = eb.stratified_delta_ci(r, flag, np.ones(n, dtype=bool), s, rng.integers(0, 200, n), n_boot=2000, seed=1)
    share = np.array([flag[s == j].mean() for j in range(4)])
    w = np.bincount(s, flag.astype(float), minlength=4)
    expect = float(np.sum(w * 0.3 * (1 - share)) / w.sum())
    assert abs(ci["d"] - expect) < 0.04 and ci["lo"] > 0 and ci["p"] < 0.01
