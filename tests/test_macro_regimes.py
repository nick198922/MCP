"""IDEA-20261017-macro B1: analytic expectations for the macro-regime helpers."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.macro_regimes import (
    aligned_changes,
    beta,
    complete_rows,
    episodes,
    fed_phase,
    h3_verdict,
    moving_block_bootstrap,
    ny_close_ms,
    ny_time_ms,
    ols_r2,
    parse_fed_changes,
    parse_nasdaq_historical,
    parse_treasury_csv,
    pearson,
    price_at,
    rolling_corr,
    sign_rule,
    thanksgiving,
    us_early_close,
)


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def test_ny_close_dst_and_early_close():
    assert _utc(ny_close_ms(date(2024, 7, 10))).hour == 20  # EDT, UTC-4
    assert _utc(ny_close_ms(date(2024, 1, 10))).hour == 21  # EST, UTC-5
    assert thanksgiving(2024) == date(2024, 11, 28)
    assert us_early_close(date(2024, 11, 29))
    assert _utc(ny_close_ms(date(2024, 11, 29))).hour == 18  # 13:00 EST
    assert us_early_close(date(2024, 12, 24)) and not us_early_close(date(2022, 12, 24 - 1))  # 2022-12-23 is a Fri
    assert not us_early_close(date(2020, 7, 3))  # Friday, market closed (holiday observed); not an early close
    assert _utc(ny_time_ms(date(2024, 3, 11), 9)).hour == 13  # first Monday of EDT


def test_price_at_uses_bar_ending_at_t():
    t = ny_close_ms(date(2024, 7, 10))
    bars = {t - 3_600_000: 101.0, t: 102.0}
    assert price_at(bars, t) == 101.0
    assert price_at(bars, t + 7_200_000) is None


def test_parsers():
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {"date": "11/19/2021", "close": "16,057.44", "open": "15,997.18"},
                    {"date": "11/18/2021", "close": "$15,993.71", "open": "N/A"},
                    {"date": "11/17/2021", "close": "N/A", "open": "1"},
                ]
            }
        }
    }
    p = parse_nasdaq_historical(payload)
    assert p[date(2021, 11, 19)] == (15997.18, 16057.44)
    assert p[date(2021, 11, 18)] == (None, 15993.71)
    assert date(2021, 11, 17) not in p
    csv_text = 'Date,"1 Mo","2 Yr","10 Yr"\n10/16/2026,4.12,4.74,5.09\n10/15/2026,4.10,,5.00\n'
    t = parse_treasury_csv(csv_text)
    assert t[date(2026, 10, 16)] == pytest.approx((0.0474, 0.0509))
    assert t[date(2026, 10, 15)][0] is None
    html = (
        "<h4>2025</h4><table><thead><tr><th>Date</th></tr></thead><tbody>"
        "<tr><td>December 11</td><td>0</td><td>25</td><td>3.50-3.75</td></tr>"
        "<tr><td>September 18</td><td>0</td><td>25</td><td>4.00-4.25</td></tr></tbody></table>"
        "<h4>2022</h4><table><tbody><tr><td>March 17</td><td>25</td><td>0</td><td>0.25-0.50</td></tr></tbody></table>"
    )
    ch = parse_fed_changes(html)
    assert ch == [(date(2022, 3, 17), 0.0025), (date(2025, 9, 18), -0.0025), (date(2025, 12, 11), -0.0025)]


def test_aligned_changes_and_missing():
    days = [date(2024, 1, d) for d in (2, 3, 4, 5)]
    v = {days[0]: 100.0, days[1]: 110.0, days[3]: 121.0}
    lr = aligned_changes(days, v)
    assert lr[0] == pytest.approx(math.log(1.1))
    assert lr[1] is None and lr[2] is None
    d = aligned_changes(days, {days[0]: 0.04, days[1]: 0.0425, days[2]: 0.0425, days[3]: 0.04}, kind="diff")
    assert d == pytest.approx([0.0025, 0.0, -0.0025])
    assert list(complete_rows([1.0, None, 2.0], [1.0, 1.0, float("nan")])) == [0]


def test_pearson_beta_r2_analytic():
    rng = np.random.default_rng(1)
    x = rng.normal(size=500)
    z = rng.normal(size=500)
    y = 2.0 * x + 0.5
    assert pearson(x, y) == pytest.approx(1.0)
    assert beta(y, x) == pytest.approx(2.0)
    assert ols_r2(y, [x]) == pytest.approx(1.0)
    y2 = x + z  # R^2 on x alone ~ 0.5, on both = 1
    assert ols_r2(y2, [x, z]) == pytest.approx(1.0)
    assert 0.4 < ols_r2(y2, [x]) < 0.6
    assert math.isnan(pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))


def test_rolling_corr_is_causal():
    rng = np.random.default_rng(2)
    x = list(rng.normal(size=200))
    y = list(rng.normal(size=200))
    a = rolling_corr(x, y, window=60)
    y_changed = y[:150] + list(rng.normal(size=50) * 10)
    b = rolling_corr(x, y_changed, window=60)
    assert a[:150] == b[:150]  # results before t do not change when data after t change
    assert a[58] is None and a[59] is not None
    assert a[59] == pytest.approx(pearson(x[:60], y[:60]))


def test_fed_phase_no_lookahead():
    ch = [(date(2022, 3, 17), 0.0025), (date(2023, 7, 27), 0.0025), (date(2024, 9, 19), -0.005)]
    assert fed_phase(ch, date(2022, 3, 16)) == "pause"
    assert fed_phase(ch, date(2022, 3, 17)) == "hike"
    assert fed_phase(ch, date(2023, 11, 23)) == "hike"  # 119 days after 2023-07-27
    assert fed_phase(ch, date(2023, 11, 25)) == "pause"
    assert fed_phase(ch, date(2023, 11, 25), window_days=180) == "hike"
    assert fed_phase(ch, date(2024, 9, 18)) == "pause"
    assert fed_phase(ch, date(2024, 9, 19)) == "cut"
    # adding a future change does not alter earlier phases
    ch2 = ch + [(date(2026, 9, 17), 0.0025)]
    days = [date(2022, 1, 1), date(2023, 1, 1), date(2024, 10, 1), date(2026, 9, 16)]
    assert [fed_phase(ch, d) for d in days] == [fed_phase(ch2, d) for d in days]
    assert episodes(["a", "a", "b", "a"]) == [("a", 0, 1), ("b", 2, 2), ("a", 3, 3)]


def test_block_bootstrap_mean():
    rng = np.random.default_rng(3)
    v = rng.normal(0.01, 0.02, size=2000)
    out = moving_block_bootstrap(len(v), lambda idx: float(v[idx].mean()), block=20, reps=400)
    lo, hi = out["ci95"]
    assert lo < float(v.mean()) < hi
    assert out["se"] == pytest.approx(0.02 / math.sqrt(2000), rel=0.35)
    assert moving_block_bootstrap(5, lambda idx: 0.0)["ci95"] is None


def test_sign_rule():
    r = [math.log(1.01), math.log(0.98), math.log(1.02)]
    out = sign_rule([1.0, 1.0, -1.0], r, cost=0.0005)
    assert out == pytest.approx([0.01 - 0.0005, -0.02, -0.02 - 0.001])
    assert sign_rule([0.0], [0.05]) == [0.0]


def test_h3_verdict_follows_registered_rule():
    nan = float("nan")
    # ✓ needs lo > 0, every hike episode positive and >= 2 episodes
    assert h3_verdict(0.5, 0.1, 0.9, [0.3, 0.4]) == "✓"
    assert h3_verdict(0.5, 0.1, 0.9, [0.3]) == "≈"  # fewer than 2 episodes: at most ≈
    assert h3_verdict(0.5, 0.1, 0.9, [0.3, -0.2]) == "◯"  # lo > 0 but episode signs disagree
    # CI spans 0: ≈ only if the point estimate and every episode agree in sign
    assert h3_verdict(0.1, -0.2, 0.4, [0.2, 0.1]) == "≈"
    # review 2026-10-18, BTC: +0.01 [-0.41; +0.40], episodes -0.54 / +0.25 -> ◯ (first run said ≈)
    assert h3_verdict(0.01, -0.41, 0.40, [-0.54, 0.25]) == "◯"
    # review 2026-10-18, ETH: -0.24 [-0.84; +0.28], episodes -0.93 / +0.07 -> ◯ (first run said "✗ (opposite sign)")
    assert h3_verdict(-0.24, -0.84, 0.28, [-0.93, 0.07]) == "◯"
    # ✗ only when the upper bound is below 0
    assert h3_verdict(-0.5, -0.9, -0.1, [-0.3, 0.2]) == "✗"
    # no episodes or no CI: never ✓ / ✗
    assert h3_verdict(0.3, 0.1, 0.5, []) == "◯"
    assert h3_verdict(0.3, nan, nan, [0.2, 0.2]) == "≈"
    assert h3_verdict(-0.3, nan, nan, [0.2, 0.2]) == "◯"
