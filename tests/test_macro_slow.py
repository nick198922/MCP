"""Tests for analytics.macro_slow (IDEA-20261017-macro, B3): analytic references and no-lookahead checks."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.macro_slow import (
    add_months,
    block_bootstrap,
    candle_month_close,
    changes,
    circular_shift_null,
    exposure_alpha,
    lagged_pairs,
    mde,
    month_end_values,
    month_range,
    parse_frb_series,
    phase_verdict,
    predictive_verdict,
    rule_alpha_stat,
    spearman,
    timing_positions,
    timing_returns,
)

# Structure copied from the Fed Data Download Program H.6 / H.10 packages (2026-09-14 / 2026-10 downloads); values cut.
FRB_XML = """<kf:Series ADJUSTED="SA" CURRENCY="USD" FREQ="129" SERIES_NAME="M2.WM" UNIT="Currency" UNIT_MULT="1e+09">
      <frb:Obs OBS_STATUS="A" OBS_VALUE="19402.1" TIME_PERIOD="2021-01-25" />
    </kf:Series>
    <kf:Series ADJUSTED="SA" CURRENCY="USD" FREQ="129" SERIES_NAME="M2.M" UNIT="Currency" UNIT_MULT="1e+09">
      <frb:Annotations>
        <common:Annotation>
          <common:AnnotationType>Short Description</common:AnnotationType>
          <common:AnnotationText>M2; Seasonally adjusted</common:AnnotationText>
        </common:Annotation>
      </frb:Annotations>
      <frb:Obs OBS_STATUS="A" OBS_VALUE="286.6" TIME_PERIOD="1959-01-31" />
      <frb:Obs OBS_STATUS="A" OBS_VALUE="287.7" TIME_PERIOD="1959-02-28" />
      <frb:Obs OBS_STATUS="ND" OBS_VALUE="-9999" TIME_PERIOD="1959-03-31" />
    </kf:Series>"""


def test_parse_frb_series_picks_named_series_and_drops_nd():
    s = parse_frb_series(FRB_XML, "M2.M")
    assert s == {date(1959, 1, 31): 286.6, date(1959, 2, 28): 287.7}
    assert parse_frb_series(FRB_XML, "M2.WM") == {date(2021, 1, 25): 19402.1}
    assert parse_frb_series(FRB_XML, "NOPE") == {}


def test_months_helpers():
    assert add_months(date(2020, 11, 1), 3) == date(2021, 2, 1)
    assert add_months(date(2020, 1, 1), -1) == date(2019, 12, 1)
    assert month_range(date(2020, 11, 15), date(2021, 2, 3)) == [date(2020, 11, 1), date(2020, 12, 1), date(2021, 1, 1), date(2021, 2, 1)]


def test_month_end_values_last_observation_inside_month():
    daily = {date(2020, 1, 30): 1.0, date(2020, 1, 31): 2.0, date(2020, 2, 3): 3.0, date(2020, 2, 28): None, date(2020, 2, 27): 4.0}
    assert month_end_values(daily) == {date(2020, 1, 1): 2.0, date(2020, 2, 1): 4.0}


def test_month_end_values_no_lookahead():
    base = {date(2020, m, d): float(m * 100 + d) for m in (1, 2, 3) for d in (5, 20)}
    a = month_end_values(base)
    later = dict(base)
    later[date(2020, 3, 25)] = 999.0  # data after February changes
    later[date(2020, 4, 2)] = 5.0
    b = month_end_values(later)
    assert {k: v for k, v in a.items() if k <= date(2020, 2, 1)} == {k: v for k, v in b.items() if k <= date(2020, 2, 1)}


def test_candle_month_close_uses_last_daily_candle():
    def ms(d: date) -> int:
        return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)

    rows = [[ms(date(2020, 1, 30)), 0, 0, 0, 10.0, 0], [ms(date(2020, 1, 31)), 0, 0, 0, 11.0, 0], [ms(date(2020, 2, 1)), 0, 0, 0, 12.0, 0]]
    assert candle_month_close(rows) == {date(2020, 1, 1): 11.0, date(2020, 2, 1): 12.0}


def test_changes_log_diff_span():
    v = {date(2020, 1, 1): 100.0, date(2020, 2, 1): 110.0, date(2020, 4, 1): 121.0}
    lg = changes(v)
    assert lg == {date(2020, 2, 1): pytest.approx(math.log(1.1))}  # March missing -> no April change
    assert changes(v, kind="diff") == {date(2020, 2, 1): pytest.approx(10.0)}
    assert changes(v, span=3) == {date(2020, 4, 1): pytest.approx(math.log(1.21))}


def test_lagged_pairs_alignment():
    sig = {date(2020, 1, 1): 1.0, date(2020, 2, 1): 2.0, date(2020, 3, 1): 3.0}
    ret = {date(2020, 3, 1): 0.3, date(2020, 4, 1): 0.4, date(2020, 5, 1): 0.5}
    months = month_range(date(2020, 3, 1), date(2020, 5, 1))
    assert lagged_pairs(sig, ret, months, 2) == [(date(2020, 3, 1), 1.0, 0.3), (date(2020, 4, 1), 2.0, 0.4), (date(2020, 5, 1), 3.0, 0.5)]
    assert lagged_pairs(sig, ret, months, 0) == [(date(2020, 3, 1), 3.0, 0.3)]


def test_spearman_monotone_and_ties():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert spearman(x, [math.exp(v) for v in x]) == pytest.approx(1.0)
    assert spearman(x, [-v for v in x]) == pytest.approx(-1.0)
    # ties: ranks averaged -> equals Pearson of average ranks
    assert spearman([1.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(np.corrcoef([0.5, 0.5, 2, 3], [0, 1, 2, 3])[0, 1])


def test_mde():
    assert mde(0.1) == pytest.approx(0.28)
    assert math.isnan(mde(None))


@pytest.mark.parametrize(
    ("stat", "ci", "half2", "sign", "m", "want"),
    [
        (0.3, (0.1, 0.5), 0.2, +1, 0.2, "✓"),
        (0.3, (0.1, 0.5), -0.1, +1, 0.2, "≈"),
        (-0.3, (-0.5, -0.1), -0.2, -1, 0.2, "✓"),
        (0.3, (0.1, 0.5), 0.2, -1, 0.2, "✗ протилежний знак"),
        (0.05, (-0.1, 0.2), 0.1, +1, 0.25, "✗"),
        (0.05, (-0.1, 0.2), 0.1, +1, 0.35, "◯"),
        (0.05, None, 0.1, +1, 0.2, "◯"),
    ],
)
def test_predictive_verdict(stat, ci, half2, sign, m, want):
    assert predictive_verdict(stat, ci, half2, sign, m) == want


def test_phase_verdict():
    assert phase_verdict(0.05, (0.01, 0.09), 2, 3, [0.02, 0.03], 0.04) == "✓"
    assert phase_verdict(0.05, (0.01, 0.09), 1, 3, [0.02, 0.03], 0.04) == "≈"
    assert phase_verdict(0.05, (0.01, 0.09), 2, 3, [0.02, float("nan")], 0.04) == "≈"
    assert phase_verdict(-0.05, (-0.09, -0.01), 2, 2, [-0.1, -0.1], 0.04) == "✗ протилежний знак"
    assert phase_verdict(0.0, (-0.03, 0.03), 2, 2, [0.0, 0.0], 0.04) == "✗"
    assert phase_verdict(0.0, (-0.1, 0.1), 3, 3, [0.0, 0.0], 0.14) == "◯"


def test_timing_positions_median_rule():
    months = month_range(date(2000, 1, 1), date(2000, 12, 1))
    sig = {add_months(date(2000, 1, 1), k): float(k) for k in range(-10, 12)}  # increasing signal
    pos = timing_positions(sig, [date(2000, 6, 1)], lag=2, window=4, min_hist=4)
    # signal[m-2] = 3 (April 2000 = index 3); history Dec..Mar = -1..2 -> median 0.5 -> long
    assert pos[date(2000, 6, 1)] == 1
    sig_dn = {k: -v for k, v in sig.items()}
    assert timing_positions(sig_dn, [date(2000, 6, 1)], lag=2, window=4, min_hist=4)[date(2000, 6, 1)] == 0
    short = timing_positions({date(2000, 4, 1): 1.0}, months, lag=2, window=4, min_hist=4)
    assert all(v is None for v in short.values())


def test_timing_positions_no_lookahead():
    rng = np.random.default_rng(1)
    keys = [add_months(date(2000, 1, 1), k) for k in range(120)]
    sig = {k: float(v) for k, v in zip(keys, rng.normal(size=120), strict=True)}
    months = keys[70:]
    a = timing_positions(sig, months, lag=2, window=60, min_hist=36)
    t = date(2006, 1, 1)
    later = {k: (v if k <= add_months(t, -2) else 100.0 * (1 + v)) for k, v in sig.items()}  # change signal after t-2
    b = timing_positions(later, months, lag=2, window=60, min_hist=36)
    assert {m: a[m] for m in months if m <= t} == {m: b[m] for m in months if m <= t}


def test_timing_returns_costs_and_alpha():
    lr = [math.log(1.1), math.log(0.9), math.log(1.2)]
    net = timing_returns([1, 0, 1], lr, cost=0.001)
    assert net == pytest.approx([0.1 - 0.001, -0.001, 0.2 - 0.001])
    pos = np.array([1.0, 1.0, 1.0])
    net_all = np.array(timing_returns([1, 1, 1], lr, cost=0.0))
    assert exposure_alpha(net_all, pos, np.array(lr)) == pytest.approx(0.0)
    # in-market only in the good months: mean net 0.3/3, buy-and-hold mean (0.1 - 0.1 + 0.2)/3, f = 2/3, no costs
    p = np.array([1.0, 0.0, 1.0])
    n2 = np.array(timing_returns([1, 0, 1], lr, cost=0.0))
    assert exposure_alpha(n2, p, np.array(lr)) == pytest.approx(0.1 - (2 / 3) * (0.2 / 3))
    stat = rule_alpha_stat(p, np.array(lr), 0.0)
    assert stat(np.arange(3)) == pytest.approx(exposure_alpha(n2, p, np.array(lr)))


def test_circular_shift_null():
    x = np.random.default_rng(5).normal(size=60)
    r = circular_shift_null(x, x)
    assert r["stat"] == pytest.approx(1.0) and r["p"] == pytest.approx(0.0) and r["n"] == 60
    y = np.roll(x, 30)  # the shift k = 30 realigns the series exactly, so the null contains corr = 1
    r0 = circular_shift_null(x, y)
    null = [np.corrcoef(np.roll(x, k), y)[0, 1] for k in range(6, 55)]
    assert max(null) == pytest.approx(1.0)
    assert r0["p"] == pytest.approx(np.mean(np.abs(null) >= abs(np.corrcoef(x, y)[0, 1])))
    assert r0["sd"] == pytest.approx(float(np.std(null)))
    assert math.isnan(circular_shift_null(x[:10], x[:10])["p"])


def test_block_bootstrap_constant_and_deterministic():
    x = np.full(30, 0.5)
    b = block_bootstrap(30, lambda i: float(x[i].mean()), block=6, reps=200, seed=7)
    assert b["stat"] == pytest.approx(0.5) and b["ci95"] == pytest.approx((0.5, 0.5)) and b["ci99"] == pytest.approx((0.5, 0.5))
    y = np.random.default_rng(3).normal(size=60)
    b1 = block_bootstrap(60, lambda i: float(y[i].mean()), block=6, reps=300, seed=11)
    b2 = block_bootstrap(60, lambda i: float(y[i].mean()), block=6, reps=300, seed=11)
    assert b1["ci95"] == pytest.approx(b2["ci95"])
    assert b1["ci99"][0] <= b1["ci95"][0] <= b1["stat"] <= b1["ci95"][1] <= b1["ci99"][1]
    assert block_bootstrap(4, lambda i: 0.0, block=6)["ci95"] is None
