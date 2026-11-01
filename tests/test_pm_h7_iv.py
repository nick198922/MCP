"""H7' profile on the IV model (IDEA-20261031-structural-edges-depth, item 8k): analytic expectations."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics import pm_h7_iv as h
from tradedesk.analytics import pm_modules as pm
from tradedesk.analytics.pm_threshold import YEAR_S, digital_above

FLAT = {(hh, w): 1.0 for hh in range(24) for w in (False, True)}
WKND_HALF = {(hh, w): (0.5 if w else 1.0) for hh in range(24) for w in (False, True)}


def ts(y: int, mo: int, d: int, hh: int, mi: int = 0) -> int:
    return int(datetime(y, mo, d, hh, mi, tzinfo=UTC).timestamp())


FRI16 = ts(2026, 10, 30, 16)  # Friday
SAT16 = ts(2026, 10, 31, 16)
SUN08 = ts(2026, 11, 1, 8)


def test_flat_profile_weight_is_hours_and_scale_is_one() -> None:
    assert h.window_weight_exact(FLAT, FRI16 + 1200, SAT16) == pytest.approx(24 - 1 / 3)
    assert h.profile_var_scale(FLAT, FRI16 + 1200, SAT16, SUN08) == pytest.approx(1.0)
    p = h.profile_prob(FLAT, "above", 0.3, 0.5, 100.0, 105.0, FRI16 + 1200, SAT16, SUN08)
    assert p == pytest.approx(0.3, abs=1e-12)


def test_partial_hours_are_pro_rata() -> None:
    prof = dict(FLAT)
    mon = ts(2026, 10, 26, 0)
    prof[(10, False)] = 3.0
    assert h.window_weight_exact(prof, mon + 9 * 3600 + 1800, mon + 10 * 3600 + 1800) == pytest.approx(0.5 + 1.5)


def test_matches_hourly_window_weight_on_aligned_hours() -> None:
    prof = {(hh, w): 0.4 + 0.05 * hh + (0.3 if w else 0.0) for hh in range(24) for w in (False, True)}
    for shift in (0, 24, 5):
        assert h.window_weight_exact(prof, FRI16, SUN08, shift) == pytest.approx(pm.window_weight(prof, FRI16, SUN08, shift))


def test_weekend_scale_analytic() -> None:
    # Fri 16-21 weekday (5 h x 1), Fri 21 - Sat 16 weekend (19 h x 0.5) -> W_m = 14.5 over 24 h;
    # + Sat 16 - Sun 08 weekend (16 h x 0.5) -> W_E = 22.5 over 40 h; s = (14.5 / 24) / (22.5 / 40)
    s = h.profile_var_scale(WKND_HALF, FRI16, SAT16, SUN08)
    assert s == pytest.approx((14.5 / 24) / (22.5 / 40))
    assert s > 1.0  # the expiry holds more weekend than the window, so the flat model under-allocates the window


def test_expiry_at_end_changes_nothing() -> None:
    assert h.profile_var_scale(WKND_HALF, FRI16, SAT16, SAT16) == pytest.approx(1.0)
    assert h.profile_var_scale(WKND_HALF, SAT16, FRI16, SUN08) is None  # empty window
    assert h.profile_var_scale(WKND_HALF, FRI16, SUN08, SAT16) is None  # expiry before end


def test_whole_week_shift_is_identity_and_one_day_shift_moves_weekend() -> None:
    s0 = h.profile_var_scale(WKND_HALF, FRI16, SAT16, SUN08)
    assert h.profile_var_scale(WKND_HALF, FRI16, SAT16, SUN08, shift_h=7 * 24) == pytest.approx(s0)
    assert h.profile_var_scale(WKND_HALF, FRI16, SAT16, SUN08, shift_h=24) != pytest.approx(s0)


def test_cells_outside_the_window_do_not_matter() -> None:
    # [Fri 16:00; Sun 08:00) never touches a weekday 03:00 bar
    prof = dict(WKND_HALF)
    prof[(3, False)] = 9.0
    assert h.profile_var_scale(prof, FRI16, SAT16, SUN08) == pytest.approx(h.profile_var_scale(WKND_HALF, FRI16, SAT16, SUN08))


def test_profile_prob_is_vol_scaling_by_sqrt_s() -> None:
    iv, f, k_strike = 0.5, 100.0, 103.0
    tau = (SAT16 - FRI16) / YEAR_S
    p_model = digital_above(f, k_strike, iv, tau)
    s = h.profile_var_scale(WKND_HALF, FRI16, SAT16, SUN08)
    want = digital_above(f, k_strike, iv * math.sqrt(s), tau)
    got = h.profile_prob(WKND_HALF, "above", p_model, iv, f, k_strike, FRI16, SAT16, SUN08)
    assert got == pytest.approx(want, abs=1e-12)
    assert got > p_model  # more variance -> an out-of-the-money digital is worth more


def test_gate_terms_and_expected_gain() -> None:
    t = h.gate_terms([0.5], [0.4])
    assert t["d"] == pytest.approx(0.1) and t["D"] == pytest.approx(0.01) and t["var0"] == pytest.approx(0.01)
    # E[Brier(base) - Brier(prof)] when y ~ p_prof equals (p_base - p_prof)^2
    e = 0.4 * (pm.brier(0.5, 1) - pm.brier(0.4, 1)) + 0.6 * (pm.brier(0.5, 0) - pm.brier(0.4, 0))
    assert e == pytest.approx(t["D"])
    sym = h.gate_terms([0.5, 0.5], [0.4, 0.6])
    assert sym["d"] == pytest.approx(0.0) and sym["var0"] == pytest.approx(0.0) and sym["D"] == pytest.approx(0.01)
    g = h.gate_mde([t])
    assert g["se0"] == pytest.approx(0.1) and g["mde0"] == pytest.approx(0.28) and g["target"] == pytest.approx(0.01)
    assert h.n_for_gate(0.01, 0.1, 1) == 784
    assert h.gate_mde([])["mde0"] is None
    with pytest.raises(ValueError):
        h.gate_terms([], [])
