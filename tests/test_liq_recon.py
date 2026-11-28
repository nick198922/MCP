"""analytics/liq_recon.py: reconciliation of Coinalyze liquidations with our stream rows (analytic expectations)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradedesk.analytics.liq_recon import (
    DAILY_BAND,
    DAILY_MIN_N,
    DAILY_SHARE_FULL,
    DAY,
    HOUR,
    bucket_events,
    compare_side,
    hour_coverage,
    implied_prices,
    long_share_corr,
    qualifying_days,
    qualifying_hours,
    side_test,
    stream_verdict,
    sum_hours_by_day,
    throttle_per_second,
)

FIX = Path(__file__).parent / "fixtures" / "coinalyze_liq_btc_bybit_daily_coins_2026-11-28.json"
D0 = 1795564800  # 2026-11-25 00:00 UTC


def test_bucket_events_maps_taker_side_to_position_side():
    ev = [(D0 + 10, "sell", 1.0, 100.0), (D0 + 20, "buy", 0.5, 60.0), (D0 + 3700, "sell", 2.0, 210.0), (D0 + 30, "x", 9.0, 9.0)]
    h = bucket_events(ev, HOUR)
    assert h[D0] == {"long": 1.0, "short": 0.5, "long_usd": 100.0, "short_usd": 60.0, "n": 2}
    assert h[D0 + HOUR]["long"] == 2.0 and h[D0 + HOUR]["short"] == 0.0
    d = bucket_events(ev, DAY)
    assert d[D0]["long"] == 3.0 and d[D0]["n"] == 3


def test_bucket_events_past_unchanged_by_later_data():
    base = [(D0 + i * 600, "sell" if i % 2 else "buy", 0.1 * i, 10.0 * i) for i in range(1, 200)]
    t = D0 + 2 * DAY
    a = bucket_events(base, HOUR, until=t)
    b = bucket_events(base + [(t + 5, "sell", 50.0, 1.0), (t + 9000, "buy", 7.0, 1.0)], HOUR, until=t)
    assert a == b
    # without the cut, later rows only add later buckets: earlier buckets stay identical
    c = bucket_events(base + [(t + 5, "sell", 50.0, 1.0)], HOUR)
    assert {k: v for k, v in c.items() if k < t} == a


def test_hour_coverage_sums_and_caps():
    assert hour_coverage([(D0 + 5, 1000.0), (D0, 3000.0), (D0 + HOUR, 3600.0)]) == {D0: 3600.0, D0 + HOUR: 3600.0}


def test_qualifying_days_rules():
    full = {D0 + i * HOUR: 3600.0 for i in range(24)}
    thin = {**{D0 + DAY + i * HOUR: 3300.0 for i in range(24)}}  # 91.7 % of the day
    holey = {**{D0 + 2 * DAY + i * HOUR: 3600.0 for i in range(24)}, D0 + 2 * DAY + 5 * HOUR: 2000.0}  # one hour < 60 %
    low = {D0 + 3 * DAY + i * HOUR: 3200.0 for i in range(24)}  # 88.9 %
    short = {D0 + 4 * DAY + i * HOUR: 3600.0 for i in range(23)}  # an hour missing from the journal
    q = qualifying_days({**full, **thin, **holey, **low, **short}, D0, D0 + 4 * DAY)
    assert set(q) == {D0, D0 + DAY}
    assert q[D0] == pytest.approx(1.0) and q[D0 + DAY] == pytest.approx(3300 / 3600)


def test_qualifying_hours_end_and_threshold():
    hrs = {D0: 3600.0, D0 + HOUR: 3539.0, D0 + 2 * HOUR: 3540.0, D0 + 3 * HOUR: 3600.0}
    assert qualifying_hours(hrs, D0 + 3 * HOUR) == {D0: 1.0, D0 + 2 * HOUR: pytest.approx(3540 / 3600)}


def test_compare_side_coverage_adjusted():
    ref = {D0 + i * DAY: 100.0 for i in range(10)}
    cov = {k: 0.9 for k in ref}
    ours = {k: 90.0 for k in ref}  # exactly coverage x ref
    s = compare_side(ours, ref, cov, eps=0.05, band=DAILY_BAND)
    assert s["n"] == 10 and s["informative"] == 10 and s["agree"] == 10 and s["above"] == 0
    assert s["ratio"] == pytest.approx(0.9) and s["ratio_cov"] == pytest.approx(1.0) and s["median_ratio"] == pytest.approx(1.0)
    assert s["corr"] is None  # constant series: no correlation
    assert stream_verdict(s, lower_bound=False, min_n=DAILY_MIN_N, share_full=DAILY_SHARE_FULL) == "✓"


def test_compare_side_missing_bucket_is_zero_and_eps():
    ref = {D0: 0.001, D0 + DAY: 10.0}
    cov = {D0: 1.0, D0 + DAY: 1.0, D0 + 2 * DAY: 1.0}  # third day absent from ref: not compared
    s = compare_side({}, ref, cov, eps=0.005, band=0.1)
    assert s["n"] == 2 and s["informative"] == 1 and s["agree"] == 0 and s["sum_ours"] == 0.0
    assert s["first"] == D0 and s["last"] == D0 + DAY


def _stats(ratio_cov: float, share: float, corr: float | None, above: float = 0.0, inf: int = 14) -> dict:
    return {"informative": inf, "share": share, "ratio_cov": ratio_cov, "corr": corr, "above_share": above}


def test_stream_verdict_marks():
    kw = {"min_n": 10, "share_full": 0.8}
    assert stream_verdict(_stats(1.0, 0.9, 0.99, inf=9), lower_bound=False, **kw) == "◯"
    assert stream_verdict(_stats(1.04, 0.85, 0.99), lower_bound=False, **kw) == "✓"
    assert stream_verdict(_stats(1.10, 0.5, 0.95), lower_bound=False, **kw) == "≈"
    assert stream_verdict(_stats(1.30, 0.5, 0.95), lower_bound=False, **kw) == "✗"
    assert stream_verdict(_stats(1.10, 0.5, 0.85), lower_bound=False, **kw) == "✗"
    # lower bound: ours far below Coinalyze but never above it and correlated -> ≈; above too often -> ✗
    assert stream_verdict(_stats(0.4, 0.0, 0.9, above=0.0), lower_bound=True, **kw) == "≈"
    assert stream_verdict(_stats(0.4, 0.0, 0.9, above=0.2), lower_bound=True, **kw) == "✗"
    assert stream_verdict(_stats(0.4, 0.0, 0.5), lower_bound=True, **kw) == "✗"


def test_side_test_detects_swap():
    ref = {D0: {"long": 10.0, "short": 1.0}, D0 + DAY: {"long": 2.0, "short": 8.0}, D0 + 2 * DAY: {"long": 5.0, "short": 5.0}}
    direct = {D0: {"long": 9.5, "short": 1.0}, D0 + DAY: {"long": 2.0, "short": 7.0}}
    swapped = {D0: {"long": 1.0, "short": 9.5}, D0 + DAY: {"long": 7.0, "short": 2.0}}
    assert side_test(direct, ref, ref, 0.05) == {"informative": 2, "direct_closer": 2, "swapped_closer": 0}
    assert side_test(swapped, ref, ref, 0.05) == {"informative": 2, "direct_closer": 0, "swapped_closer": 2}


def test_long_share_corr_sign():
    ref = {D0 + i * DAY: {"long": float(i + 1), "short": float(10 - i)} for i in range(6)}
    frac = {k: {"long": 0.2 * v["long"], "short": 0.2 * v["short"]} for k, v in ref.items()}  # 20 % of the flow, same mix
    crossed = {k: {"long": v["short"], "short": v["long"]} for k, v in frac.items()}
    assert long_share_corr(frac, ref, ref, 0.05) == {"n": 6, "corr": pytest.approx(1.0)}
    assert long_share_corr(crossed, ref, ref, 0.05)["corr"] == pytest.approx(-1.0)


def test_throttle_keeps_last_row_per_second():
    ev = [(D0, "sell", 1.0, 1.0), (D0, "buy", 2.0, 2.0), (D0 + 1, "sell", 3.0, 3.0), (D0, "sell", 4.0, 4.0)]
    assert throttle_per_second(ev) == [(D0, "sell", 4.0, 4.0), (D0 + 1, "sell", 3.0, 3.0)]


def test_sum_hours_by_day_needs_all_hours():
    hourly = {D0 + i * HOUR: 1.5 for i in range(24)} | {D0 + DAY + i * HOUR: 1.0 for i in range(23)}
    assert sum_hours_by_day(hourly, [D0, D0 + DAY]) == {D0: pytest.approx(36.0)}


def test_implied_prices():
    assert implied_prices({D0: 900.0, D0 + DAY: 5.0}, {D0: 0.01, D0 + DAY: 0.001}, 0.005) == {D0: pytest.approx(90000.0)}


def test_real_sample_rows_bucket_by_day():
    """The trimmed live answer (2026-11-28): rows are UTC day starts in coins, long/short as the tool reports them."""
    rows = json.loads(FIX.read_text())["data"]["rows"]
    assert all(r["ts"] % DAY == 0 for r in rows)
    ref = {r["ts"]: {"long": r["long"], "short": r["short"]} for r in rows}
    assert ref[D0]["long"] == pytest.approx(248.39) and ref[D0]["short"] == pytest.approx(155.875)
    # our stream equal to it on the three closed days, the open day excluded by coverage
    ours = {k: dict(v) for k, v in ref.items()}
    cov = {D0: 1.0, D0 + DAY: 1.0, D0 + 2 * DAY: 1.0}
    s = compare_side({k: v["long"] for k, v in ours.items()}, {k: v["long"] for k, v in ref.items()}, cov, 0.005, 0.1)
    assert s["n"] == 3 and s["agree"] == 3 and s["ratio_cov"] == pytest.approx(1.0)
    assert side_test(ours, ref, cov, 0.005)["direct_closer"] == 3
