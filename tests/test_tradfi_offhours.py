from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.tradfi_offhours import (
    MIN_MS,
    WINDOWS,
    center_by_date,
    et_ms,
    mde,
    night_record,
    nights,
    nyse_sessions,
    pairwise_corr,
    participation_ratio,
    placebo_record,
    premium_slope,
    price_at,
    random_basket_sd,
    volume_between,
    window_label,
    window_labels,
)


def utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def test_sessions_dst_holidays_early_close():
    ss = {s.day: s for s in nyse_sessions(date(2026, 3, 5), date(2026, 3, 10))}
    assert date(2026, 3, 7) not in ss and date(2026, 3, 8) not in ss  # weekend
    assert utc(ss[date(2026, 3, 6)].open_ms).hour == 14  # EST: 09:30 = 14:30 UTC
    assert utc(ss[date(2026, 3, 9)].open_ms).hour == 13  # EDT from 8 March: 13:30 UTC
    assert utc(ss[date(2026, 3, 9)].close_ms).hour == 20
    gf = {s.day for s in nyse_sessions(date(2026, 4, 1), date(2026, 4, 7))}
    assert date(2026, 4, 3) not in gf and date(2026, 4, 2) in gf  # Good Friday
    nov = {s.day: s for s in nyse_sessions(date(2026, 11, 25), date(2026, 11, 30))}
    assert date(2026, 11, 26) not in nov
    assert nov[date(2026, 11, 27)].early and utc(nov[date(2026, 11, 27)].close_ms).hour == 18  # 13:00 EST
    assert not nov[date(2026, 11, 30)].early


def test_nights_weekend_and_holiday_flags():
    ss = nyse_sessions(date(2026, 9, 3), date(2026, 9, 9))  # Labor Day 7 Sep
    ns = nights(ss)
    assert [n.day for n in ns] == [date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)]
    assert [n.weekend for n in ns] == [False, True, False]
    n = ns[1]
    assert n.close_ms == et_ms(date(2026, 9, 4), 16, 0)
    assert n.pre_ms == et_ms(date(2026, 9, 8), 9, 25)
    assert n.exit_ms == et_ms(date(2026, 9, 8), 10, 30)


def _series(t0: int, n: int, price: float = 100.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ts = np.arange(n, dtype=float) * MIN_MS + t0
    return ts, np.full(n, price), np.ones(n)


def test_price_at_is_as_of_and_stale_guard():
    ts = np.array([0, 1, 2, 10], dtype=float) * MIN_MS
    close = np.array([1.0, 2.0, 3.0, 4.0])
    assert price_at(ts, close, 2 * MIN_MS) == 2.0  # candle opened at 1 min ended at 2 min
    assert price_at(ts, close, 2 * MIN_MS + 30_000) == 2.0  # candle 2 has not ended yet
    assert price_at(ts, close, 0) is None
    assert price_at(ts, close, 9 * MIN_MS) is None  # last ended candle at 3 min: 6 min stale > 5
    assert price_at(ts, close, 8 * MIN_MS) == 3.0


def test_night_record_analytic_and_no_lookahead():
    ss = nyse_sessions(date(2026, 9, 8), date(2026, 9, 9))
    n = nights(ss)[0]
    t0 = n.close_ms - 60 * MIN_MS
    ts, close, qv = _series(t0, int((n.exit_ms - t0) / MIN_MS) + 60)
    i_close = int((n.close_ms - MIN_MS - t0) / MIN_MS)  # candle ending at the close
    i_pre = int((n.pre_ms - MIN_MS - t0) / MIN_MS)
    i_exit = int((n.exit_ms - MIN_MS - t0) / MIN_MS)
    close[i_close + 1 : i_pre + 1] = 102.0  # +2 % overnight
    close[i_pre + 1 :] = 101.0
    r = night_record(ts, close, qv, n)
    assert r is not None
    assert r["D"] == pytest.approx(0.02)
    assert r["R"] == pytest.approx(101.0 / 102.0 - 1)
    assert r["vol_closed"] == pytest.approx(i_pre - i_close)  # one unit per closed-hours candle
    # changing anything after the entry time leaves the signal (and closed-hours volume) unchanged
    close2 = close.copy()
    close2[i_pre + 1 :] = 50.0
    qv2 = qv.copy()
    qv2[i_pre + 1 :] = 999.0
    r2 = night_record(ts, close2, qv2, n)
    assert r2 is not None and r2["D"] == r["D"] and r2["vol_closed"] == r["vol_closed"]
    assert r2["R"] != r["R"]
    assert i_exit < len(ts)


def test_night_record_missing_data():
    ss = nyse_sessions(date(2026, 9, 8), date(2026, 9, 9))
    n = nights(ss)[0]
    ts, close, qv = _series(n.close_ms + 10 * MIN_MS, 2000)  # starts after the previous close
    assert night_record(ts, close, qv, n) is None


def test_placebo_record_and_early_close():
    s = nyse_sessions(date(2026, 9, 8), date(2026, 9, 8))[0]
    t0 = et_ms(s.day, 10, 0)
    ts, close, _ = _series(t0, 7 * 60)
    close[ts >= et_ms(s.day, 11, 0)] = 110.0
    close[ts >= et_ms(s.day, 14, 55)] = 99.0
    r = placebo_record(ts, close, s)
    assert r is not None
    # P(11:00) = close of the 10:59 candle (100); P(14:55) = the 14:54 candle (110); P(16:00) = the 15:59 candle (99)
    assert r["D"] == pytest.approx(0.10)
    assert r["R"] == pytest.approx(99.0 / 110.0 - 1)
    early = nyse_sessions(date(2026, 11, 27), date(2026, 11, 27))[0]
    assert placebo_record(ts, close, early) is None


def test_volume_between():
    ts = np.arange(10, dtype=float) * MIN_MS
    qv = np.arange(10, dtype=float)
    assert volume_between(ts, qv, 2 * MIN_MS, 5 * MIN_MS) == 2 + 3 + 4
    assert volume_between(ts, qv, 5 * MIN_MS, 5 * MIN_MS) == 0.0


def test_center_by_date():
    d1, d2 = date(2026, 1, 2), date(2026, 1, 5)
    c = center_by_date([(d1, "A", 0.03), (d1, "B", 0.01), (d2, "A", 0.5)])
    assert c[(d1, "A")] == pytest.approx(0.01) and c[(d1, "B")] == pytest.approx(-0.01)
    assert (d2, "A") not in c


def test_pairwise_corr_handles_gaps():
    x = np.arange(40, dtype=float)
    mat = np.column_stack([x, 2 * x + 1, -x, np.where(x < 35, np.nan, x)])
    c = pairwise_corr(mat, min_periods=30)
    assert c[0, 1] == pytest.approx(1.0) and c[0, 2] == pytest.approx(-1.0)
    assert c[0, 3] == 0.0 and c[3, 3] == 1.0  # only 5 common rows: treated as unrelated


def test_power_helpers():
    assert mde(0.0035, 120) == pytest.approx(2.8 * 0.0035 / np.sqrt(120))
    assert mde(0.01, 0) is None
    assert participation_ratio([1, 1, 1, 1]) == pytest.approx(4.0)
    assert participation_ratio([4, 0, 0, 0]) == pytest.approx(1.0)
    flat = {date(2026, 1, d): [0.001] * 10 for d in range(1, 20)}
    sd, n = random_basket_sd(flat)
    assert n == 19 and sd == pytest.approx(0.0, abs=1e-15)
    rng = np.random.default_rng(1)
    noisy = {date(2026, 1, 1 + i % 28): list(rng.normal(0, 0.01, 50)) for i in range(28)}
    sd1, _ = random_basket_sd(noisy, reps=50)
    # 10 long + 10 short iid N(0, 1 %): per-trade mean has SD 1 % / sqrt(20)
    assert sd1 == pytest.approx(0.01 / np.sqrt(20), rel=0.35)
    assert random_basket_sd(noisy, reps=50) == random_basket_sd(noisy, reps=50)


@pytest.mark.parametrize(
    ("day", "hh", "mm", "want"),
    [
        (date(2026, 1, 18), 19, 59, "weekend"),  # Sunday before 20:00 ET
        (date(2026, 1, 18), 20, 0, "overnight"),  # Sunday 20:00: 24/5 venues open (even before a Monday holiday)
        (date(2026, 1, 19), 8, 0, "holiday"),  # MLK day, Monday holiday morning
        (date(2026, 1, 19), 21, 0, "holiday"),  # whole holiday weekday
        (date(2026, 1, 16), 19, 59, "afterhours"),  # Friday
        (date(2026, 1, 16), 20, 0, "weekend"),  # Friday 20:00 starts the weekend
        (date(2026, 1, 15), 20, 0, "overnight"),  # Thursday night
        (date(2026, 1, 16), 3, 59, "overnight"),
        (date(2026, 1, 16), 4, 0, "premarket"),
        (date(2026, 1, 16), 9, 29, "premarket"),
        (date(2026, 1, 16), 9, 30, "regular"),
        (date(2026, 1, 16), 15, 59, "regular"),
        (date(2026, 1, 16), 16, 0, "afterhours"),
        (date(2026, 11, 27), 12, 59, "regular"),  # early close 13:00 ET
        (date(2026, 11, 27), 13, 0, "afterhours"),
        (date(2026, 11, 26), 10, 0, "holiday"),  # Thanksgiving
        (date(2026, 3, 6), 9, 30, "regular"),  # EST (14:30 UTC)
        (date(2026, 3, 9), 9, 29, "premarket"),  # EDT from 8 March (13:29 UTC)
        (date(2026, 3, 9), 9, 30, "regular"),
        (date(2026, 3, 8), 19, 59, "weekend"),  # DST Sunday
        (date(2026, 3, 8), 20, 0, "overnight"),
    ],
)
def test_window_label_boundaries(day, hh, mm, want):
    assert window_label(et_ms(day, hh, mm)) == want


def test_window_labels_vectorised_matches_scalar():
    ts = np.arange(et_ms(date(2026, 3, 6), 0, 0), et_ms(date(2026, 3, 10), 0, 0), 17 * MIN_MS)
    assert list(window_labels(ts)) == [window_label(int(t)) for t in ts]
    # DST shift: 09:30 ET is 14:30 UTC on Fri 6 Mar and 13:30 UTC on Mon 9 Mar
    assert window_label(int(datetime(2026, 3, 6, 14, 29, tzinfo=UTC).timestamp() * 1000)) == "premarket"
    assert window_label(int(datetime(2026, 3, 9, 13, 30, tzinfo=UTC).timestamp() * 1000)) == "regular"


def _week():
    # Fri 16 Jan 2026 00:00 ET .. Thu 22 Jan: weekday nights, a weekend and the MLK holiday (Mon 19 Jan)
    ts = np.arange(et_ms(date(2026, 1, 16), 0, 0), et_ms(date(2026, 1, 22), 0, 0), MIN_MS).astype(float)
    rng = np.random.default_rng(7)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 5e-4, len(ts))))
    return ts, close


def test_premium_slope_frozen_and_live_index():
    ts, close = _week()
    frozen = np.log(close) - np.log(close[0])  # index frozen: premium moves 1:1 with ln(price)
    out = premium_slope(ts, close, ts, frozen)
    assert set(out) == set(WINDOWS)
    for w in WINDOWS:
        assert out[w] is not None and out[w][0] == pytest.approx(1.0) and out[w][1] >= 30
    live = premium_slope(ts, close, ts, np.zeros(len(ts)))  # live index absorbs the move: premium flat
    assert all(v is not None and v[0] == pytest.approx(0.0) for v in live.values())
    # frozen only on weekend/holiday, live otherwise -> slope 1 there, 0 elsewhere (labels by step end time)
    lab = window_labels(ts)
    dl = np.concatenate([[0.0], np.diff(np.log(close))])
    mixed = np.cumsum(np.where(np.isin(lab, ["weekend", "holiday"]), dl, 0.0))
    mo = premium_slope(ts, close, ts, mixed, step_ms=MIN_MS)
    assert mo["weekend"][0] == pytest.approx(1.0) and mo["holiday"][0] == pytest.approx(1.0)
    assert mo["regular"][0] == pytest.approx(0.0) and mo["overnight"][0] == pytest.approx(0.0)


def test_premium_slope_gaps_min_n_and_empty():
    ts, close = _week()
    frozen = np.log(close) - np.log(close[0])
    # a 1m kline missing at every 5-min grid point of the regular session: no grid step there -> None
    lab = window_labels(ts)
    drop = (lab == "regular") & (ts % (5 * MIN_MS) == 0)
    out = premium_slope(ts[~drop], close[~drop], ts, frozen)
    assert out["regular"] is None and out["weekend"][0] == pytest.approx(1.0)
    assert premium_slope(ts, close, ts, frozen, min_n=10**6) == dict.fromkeys(WINDOWS)
    assert premium_slope(np.array([]), np.array([]), ts, frozen) == dict.fromkeys(WINDOWS)
    # later data does not change the slope of an earlier window (no lookahead)
    cut = et_ms(date(2026, 1, 19), 0, 0)
    early = premium_slope(ts[ts < cut], close[ts < cut], ts[ts < cut], frozen[ts < cut])
    alt = frozen.copy()
    alt[ts >= cut] += 0.05 * np.sin(np.arange((ts >= cut).sum()))
    full = premium_slope(ts, close, ts, alt)
    assert full["weekend"] == early["weekend"]
