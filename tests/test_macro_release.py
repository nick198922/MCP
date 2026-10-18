"""IDEA-20261017-macro B2: analytic expectations for the macro-release reaction helpers."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.macro_release import (
    BAR_MS,
    DAY_MS,
    HOUR_MS,
    bootstrap_mean,
    bootstrap_two,
    chi2_sf,
    cochran_q,
    continuation_verdict,
    event_window,
    h1_verdict,
    log_ret,
    mde,
    normal_vol,
    parse_bls_schedule,
    parse_events_csv,
    parse_fomc_calendar,
    placebo_days,
    price_at,
    release_ms,
    rule_pnl,
    rule_verdict,
)


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def test_release_times_follow_us_dst():
    assert _utc(release_ms(date(2024, 7, 10), "cpi")).strftime("%H:%M") == "12:30"  # EDT
    assert _utc(release_ms(date(2024, 1, 11), "cpi")).strftime("%H:%M") == "13:30"  # EST
    assert _utc(release_ms(date(2024, 3, 8), "nfp")).strftime("%H:%M") == "13:30"  # before the 2024-03-10 switch
    assert _utc(release_ms(date(2024, 3, 20), "fomc")).strftime("%H:%M") == "18:00"  # EDT while Europe is still on winter time
    assert release_ms(date(2024, 3, 20), "fomc") % BAR_MS == 0


def _series(t0: int, *, a: float, b: float, c: float, hours_before: int = 800, hours_after: int = 30) -> dict[int, float]:
    """5m closes: before t0 the hourly log return alternates +a / -a (flat inside each hour);
    t0 -> t0+1h rises by b (log), t0+1h -> t0+24h by c, flat afterwards."""
    out: dict[int, float] = {}
    lp = 0.0
    start = t0 - hours_before * HOUR_MS
    for h in range(hours_before):
        lp += a if h % 2 == 0 else -a
        for k in range(12):
            out[start + h * HOUR_MS + k * BAR_MS] = math.exp(lp)  # bar opening inside hour h closes at lp
    # the bar opening at t0 - 5m (ending at t0) belongs to the last pre-release hour: already set above
    for k in range(hours_after * 12):
        t = t0 + k * BAR_MS
        end = t + BAR_MS
        if end <= t0 + HOUR_MS:
            x = lp + b * (end - t0) / HOUR_MS
        elif end <= t0 + DAY_MS:
            x = lp + b + c * (end - t0 - HOUR_MS) / (23 * HOUR_MS)
        else:
            x = lp + b + c
        out[t] = math.exp(x)
    return out


T0 = release_ms(date(2024, 7, 10), "cpi")


def test_price_at_and_log_ret_use_the_bar_ending_at_t():
    closes = {T0 - BAR_MS: 100.0, T0: 101.0, T0 + HOUR_MS - BAR_MS: 110.0}
    assert price_at(closes, T0) == 100.0  # last pre-release close
    assert log_ret(closes, T0, T0 + HOUR_MS) == pytest.approx(math.log(1.1))
    assert log_ret(closes, T0, T0 + 2 * HOUR_MS) is None


def test_normal_vol_alternating_returns():
    closes = _series(T0, a=0.01, b=0.0, c=0.0)
    assert normal_vol(closes, T0) == pytest.approx(0.01, rel=1e-9)
    assert normal_vol(closes, T0, hours=168) == pytest.approx(0.01, rel=1e-9)
    # too few hours -> None
    short = {t: v for t, v in closes.items() if t >= T0 - 100 * HOUR_MS}
    assert normal_vol(short, T0) is None


def test_event_window_analytic_continuation():
    a, b, c = 0.005, 0.02, -0.03
    w = event_window(_series(T0, a=a, b=b, c=c), T0)
    assert w is not None
    assert w["R1h"] == pytest.approx(b)
    assert w["R24h"] == pytest.approx(b + c)
    assert w["s"] == 1
    assert w["Y"] == pytest.approx(c)
    assert w["C"] == pytest.approx(c)  # reversal: negative continuation
    assert w["z"] == pytest.approx(c / (a * math.sqrt(23)))
    assert w["Rs1h"] == pytest.approx(b / a)
    assert w["z_1h_4h"] == pytest.approx((c * 3 / 23) / (a * math.sqrt(3)))


def test_event_window_none_without_24h_price():
    closes = _series(T0, a=0.005, b=0.01, c=0.01, hours_after=10)
    assert event_window(closes, T0) is None


def test_no_lookahead_values_before_t_do_not_change():
    base = _series(T0, a=0.004, b=0.01, c=0.02)
    w = event_window(base, T0)
    changed = dict(base)
    for t in list(changed):
        if t >= T0 + HOUR_MS:  # bars that end after t0 + 1h
            changed[t] *= 1.37
    w2 = event_window(changed, T0)
    assert w is not None and w2 is not None
    for k in ("sigma", "sigma7", "p0", "R5m", "R30m", "R1h", "s"):
        assert w2[k] == w[k]
    assert normal_vol(changed, T0) == normal_vol(base, T0)
    # changes after t0 + 24h leave every registered quantity unchanged
    later = {t: (v * 2.0 if t >= T0 + DAY_MS else v) for t, v in base.items()}
    w3 = event_window(later, T0)
    assert w3 is not None
    for k in ("R24h", "Y", "C", "z", "z_2h_24h"):
        assert w3[k] == w[k]


def test_no_lookahead_pre_release_quantities_ignore_first_hour():
    # every bar that opened at t0 or later (i.e. (t0, t0+1h] and beyond) changes: sigma, sigma7, p0 must not move
    base = _series(T0, a=0.004, b=0.01, c=0.02)
    w = event_window(base, T0)
    changed = {t: (v * (1.0 + 0.001 * ((t - T0) // BAR_MS + 1)) if t >= T0 else v) for t, v in base.items()}
    w2 = event_window(changed, T0)
    assert w is not None and w2 is not None
    for k in ("sigma", "sigma7", "p0"):
        assert w2[k] == w[k]
    assert w2["R5m"] != w["R5m"]  # the change did reach the first post-release bar


def test_rule_pnl_costs_per_side():
    assert rule_pnl(1, math.log(1.02), cost=0.0005) == pytest.approx(0.02 - 0.001)
    assert rule_pnl(-0.3, math.log(1.02), cost=0.0005) == pytest.approx(-0.02 - 0.001)
    assert rule_pnl(0, 0.05) == 0.0


def test_placebo_days_exclude_neighbours():
    days = [date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10)]
    out = placebo_days(days, [date(2024, 1, 4)])
    assert out == [date(2024, 1, 2), date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)]


def test_parse_events_csv_reads_original_date():
    text = (
        "date,kind,time_et,status,verification,release_url,calendar_url,note\n"
        "2025-10-24,cpi,08:30,actual,verified_bls_archive,u,c,scheduled 2025-10-15; delayed by the federal shutdown\n"
        "2025-11-07,nfp,08:30,cancelled,verified_bls_archive,,c,not published\n"
    )
    ev = parse_events_csv(text)
    assert ev[0].day == date(2025, 10, 24) and ev[0].original == date(2025, 10, 15) and ev[0].kind == "cpi"
    assert ev[1].status == "cancelled" and ev[1].original is None


FOMC_HTML = """
<div class="panel-heading"><h4><a id="1">2024 FOMC Meetings</a></h4></div>
<div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4">30-31</div>
<div class="fomc-meeting--shaded fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4">30-1</div>
<div class="fomc-meeting__month col-xs-5"><strong>June</strong></div>
<div class="fomc-meeting__date col-xs-4">11-12*</div>
<div class="panel-heading"><h4><a id="2">2025 FOMC Meetings</a></h4></div>
<div class="fomc-meeting__month col-xs-5"><strong>August</strong></div>
<div class="fomc-meeting__date col-xs-4">22 (notation vote)</div>
<div class="fomc-meeting__month col-xs-5"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4">16-17*</div>
"""


def test_parse_fomc_calendar():
    out = parse_fomc_calendar(FOMC_HTML)
    assert out[2024] == [date(2024, 1, 31), date(2024, 5, 1), date(2024, 6, 12)]
    assert out[2025] == [date(2025, 9, 17)]


def test_parse_bls_schedule():
    html = """<table>
    <tr><td>Friday, January 05, 2024</td><td>08:30 AM</td><td>Employment Situation for December 2023</td></tr>
    <tr><td>Thursday, January 11, 2024</td><td>08:30 AM</td><td>Consumer Price Index for December 2023</td></tr>
    <tr><td>Thursday, January 11, 2024</td><td>08:30 AM</td><td>Real Earnings for December 2023</td></tr>
    <tr><td>Wednesday, January 10, 2024</td><td>10:00 AM</td><td>Consumer Price Index Average Price Data</td></tr>
    </table>"""
    out = parse_bls_schedule(html, 2024)
    assert out == {"cpi": [date(2024, 1, 11)], "nfp": [date(2024, 1, 5)]}


def test_chi2_and_cochran_q():
    assert chi2_sf(3.0, 2) == pytest.approx(math.exp(-1.5), rel=1e-9)
    assert chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(30.0, 4) == pytest.approx(4.89e-6, rel=1e-2)
    q = cochran_q([0.1, 0.1, 0.1], [0.1, 0.2, 0.3])
    assert q["Q"] == pytest.approx(0.0, abs=1e-12) and q["I2"] == 0.0 and q["p"] == pytest.approx(1.0)
    q2 = cochran_q([0.0, 1.0], [0.5, 0.5])  # pooled 0.5, Q = 2 * (0.5 / 0.5)^2
    assert q2["Q"] == pytest.approx(2.0) and q2["df"] == 1 and q2["I2"] == pytest.approx(0.5)


def test_bootstraps_are_seeded_and_cover_the_mean():
    rng = np.random.default_rng(1)
    x = rng.normal(0.5, 1.0, 400)
    b1, b2 = bootstrap_mean(x), bootstrap_mean(x)
    assert b1["ci95"] == b2["ci95"]
    lo, hi = b1["ci95"]
    assert lo < float(x.mean()) < hi and lo > 0.3 and hi < 0.7
    r = bootstrap_two(np.full(50, 2.0) + rng.normal(0, 0.01, 50), np.ones(80) + rng.normal(0, 0.01, 80), lambda a, b: a.mean() / b.mean())
    assert r["stat"] == pytest.approx(2.0, rel=0.01) and r["ci95"][0] < r["stat"] < r["ci95"][1]
    assert bootstrap_mean([1.0, 2.0])["ci95"] is None


def test_mde():
    assert mde(1.0, 196) == pytest.approx(0.2)


def test_h1_verdict_rule():
    assert h1_verdict(2.9, 2.0, 3.5) == "✓"
    assert h1_verdict(2.9, 1.3, 2.2) == "≈"
    assert h1_verdict(1.9, 0.8, 1.5) == "✗"
    assert h1_verdict(1.1, 0.9, 1.4) == "✓"
    assert h1_verdict(0.9, 0.5, 0.8) == "≈"


def test_continuation_verdict_rule():
    assert continuation_verdict((0.05, 0.4), (0.02, 0.3), (0.2, 0.1), 0.24, 0.3) == "✓ продовження"
    assert continuation_verdict((-0.4, -0.05), (-0.3, -0.01), (-0.2, -0.1), 0.24, 0.3) == "✓ відкат"
    assert continuation_verdict((0.05, 0.4), (-0.02, 0.3), (0.2, 0.1), 0.24, 0.3).startswith("≈ продовження")
    assert continuation_verdict((0.05, 0.4), (0.02, 0.3), (0.2, -0.1), 0.24, 0.3) == "≈ продовження (половини розходяться)"
    assert continuation_verdict((-0.1, 0.2), (-0.1, 0.2), (0.1, 0.0), 0.24, 0.3) == "✗ (не виявлено)"
    assert continuation_verdict((-0.1, 0.2), (-0.1, 0.2), (0.1, 0.0), 0.50, 0.45).startswith("◯")
    assert continuation_verdict(None, None, (), 0.1, 0.3) == "◯"


def test_rule_verdict_rule():
    assert rule_verdict((0.001, 0.01), (0.004, 0.002), 0.006, 0.01) == "✓"
    assert rule_verdict((0.001, 0.01), (0.004, -0.002), 0.006, 0.01).startswith("✗")
    assert rule_verdict((-0.004, 0.002), (0.004, 0.002), 0.006, 0.01).startswith("✗")
    assert rule_verdict((-0.004, 0.002), (0.004, 0.002), 0.012, 0.01).startswith("◯")
