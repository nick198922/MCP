"""Tests for analytics.macro_crossasset (IDEA-20261017-macro, B1b): analytic references and no-lookahead checks."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.macro_crossasset import (
    corr_stat,
    event_diff,
    forward_log_return,
    g_verdict,
    holm,
    is_uup_exdiv,
    ols_coefs,
    r_verdict,
    shift_null,
    split_index,
    trailing_rank_flag,
    trailing_z,
)
from tradedesk.analytics.macro_regimes import HOUR_MS


def test_uup_exdiv_rule_matches_known_december_distributions():
    from datetime import date

    # real log changes from the Nasdaq UUP series (cache of B1)
    for d, r in ((date(2018, 12, 24), -0.01515), (date(2019, 12, 23), -0.02036), (date(2023, 12, 18), -0.06204)):
        assert is_uup_exdiv(d, r)
    assert not is_uup_exdiv(date(2022, 12, 1), -0.01231)  # first half of December: a market move
    assert not is_uup_exdiv(date(2022, 12, 19), -0.00851)  # above the threshold
    assert not is_uup_exdiv(date(2023, 11, 20), -0.05)  # not December
    assert not is_uup_exdiv(date(2023, 12, 18), 0.02)


def test_trailing_z_uses_only_past_sd():
    # past window alternates +1/-1 -> sample SD of 4 values [1,-1,1,-1] = sqrt(4/3)
    r = [1.0, -1.0, 1.0, -1.0, 2.0]
    z = trailing_z(r, window=4, min_valid=4)
    assert z[:4] == [None, None, None, None]
    assert z[4] == pytest.approx(2.0 / math.sqrt(4 / 3))


def test_trailing_z_missing_and_min_valid():
    r = [1.0, None, -1.0, None, 3.0]
    assert trailing_z(r, window=4, min_valid=3)[4] is None
    assert trailing_z(r, window=4, min_valid=2)[4] == pytest.approx(3.0 / math.sqrt(2.0))
    assert trailing_z([0.0, 0.0, 0.0, 1.0], window=3, min_valid=3)[3] is None  # zero SD


def test_signals_do_not_change_when_future_changes():
    rng = np.random.default_rng(1)
    r = list(rng.normal(0, 0.01, 400))
    z1, f1 = trailing_z(r), trailing_rank_flag(r)
    r2 = r[:300] + list(rng.normal(0, 0.05, 100))  # different data after t = 299
    z2, f2 = trailing_z(r2), trailing_rank_flag(r2)
    assert z1[:300] == z2[:300]
    assert f1[:300] == f2[:300]


def test_trailing_rank_flag():
    r = [float(i) for i in range(10)] + [100.0, -1.0]
    f = trailing_rank_flag(r, window=10, q=0.9, min_valid=10)
    assert f[10] is True and f[11] is False and f[9] is None


def test_forward_log_return_bar_alignment():
    t = 1_700_000_000_000 - (1_700_000_000_000 % HOUR_MS)
    closes = {t - HOUR_MS: 100.0, t + 23 * HOUR_MS: 110.0}
    assert forward_log_return(closes, t, 24 * HOUR_MS) == pytest.approx(math.log(1.1))
    assert forward_log_return(closes, t, 25 * HOUR_MS) is None


def test_event_diff_and_split():
    assert event_diff([True, True, True, False, False, False], [3, 3, 3, 1, 1, 1]) == pytest.approx(2.0)
    assert math.isnan(event_diff([True, False, False, False], [1, 2, 3, 4]))
    assert split_index(100) == 75 and split_index(7) == 5


def test_shift_null_detects_alignment_and_not_noise():
    rng = np.random.default_rng(2)
    x = rng.normal(size=500)
    y = x + rng.normal(scale=0.5, size=500)
    assert shift_null(x, y, corr_stat)["p"] < 0.01
    noise = shift_null(x, rng.normal(size=500), corr_stat)
    assert noise["p"] > 0.01
    flag = x > 1.5
    yy = rng.normal(size=500) + 2.0 * flag
    res = shift_null(flag, yy, lambda s, v: event_diff(s.astype(bool), v))
    assert res["stat"] == pytest.approx(event_diff(flag, yy)) and res["p"] < 0.01


def test_holm_reference():
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])


def test_ols_coefs_exact():
    x1 = np.arange(10.0)
    x2 = np.sin(x1)
    y = 1.0 + 2.0 * x1 - 3.0 * x2
    assert ols_coefs(y, [x1, x2]) == pytest.approx([1.0, 2.0, -3.0])


def test_g_verdict_rules():
    assert g_verdict(0.01, (0.002, 0.02), 0.01, 0.01, 0.005, 0.008, 0.01) == "✓"
    assert g_verdict(0.01, (0.002, 0.02), 0.10, 0.01, 0.005, 0.008, 0.01) == "≈"
    assert g_verdict(0.01, (0.002, 0.02), 0.01, 0.01, -0.001, 0.008, 0.01) == "≈"
    assert g_verdict(-0.01, (-0.02, -0.001), 0.01, -0.01, -0.01, 0.008, 0.01) == "✗ протилежний знак"
    assert g_verdict(0.001, (-0.005, 0.007), 0.5, 0.0, 0.0, 0.008, 0.01).startswith("✗")
    assert g_verdict(0.001, (-0.005, 0.007), 0.5, 0.0, 0.0, 0.012, 0.01).startswith("◯")
    assert g_verdict(0.001, None, 0.5, 0.0, 0.0, 0.008, 0.01) == "◯"


def test_r_verdict_rules():
    assert r_verdict("✓", (0.001, 0.01), 0.002, 0.008, 0.01) == "✓"
    assert r_verdict("≈", (0.001, 0.01), 0.002, 0.008, 0.01).startswith("✗")
    assert r_verdict("◯ (ворота потужності)", (-0.01, 0.01), 0.0, 0.008, 0.01).startswith("◯")
    assert r_verdict("✗ (не виявлено)", (-0.01, 0.01), 0.0, 0.02, 0.01).startswith("◯")
