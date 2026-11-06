"""Mathematical invariants of analytics/derivatives.py and analytics/probability.py on a deterministic grid.

No network, no hypothesis. Conventions under test (docs/ARCHITECTURE.md): funding, basis and their annualised
forms are decimals (0.10 = 10 %), simple annualisation = rate x periods per year (8760 h / interval), basis
annualised over days / 365; probabilities are RISK-NEUTRAL, Black-76 with r = 0 on the expiry forward.
A silent change of a sign, of 8760 / 365, or of the Breeden-Litzenberger slope term breaks a test here
before it reaches the funding tables, the PM gap or the forecast logs.

Known degenerate cases are not hidden by tolerances: they are listed in ``DEGENERATE`` with a reason and
pinned by their own tests.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

import pytest

from tradedesk.analytics.derivatives import annualize_funding, basis, parse_interval_hours, summarise_funding_history
from tradedesk.analytics.options import Smile, black76_price, d1d2, ncdf
from tradedesk.analytics.probability import prob_above, prob_touch

NOW = datetime(2026, 11, 6, 8, tzinfo=UTC)

# ---------- grids ----------
INDEXES = (100.0, 65_000.0)
RATIOS = (0.8, 0.95, 0.999, 1.0, 1.001, 1.05, 1.25)  # mark / index, sorted ascending
BASIS_DAYS = (0.06, 1.0, 7.0, 30.0, 90.0, 365.0)  # just above the 0.05-day cut-off .. 1 year
RATES = (-0.003, -0.0001, 0.0, 0.0000125, 0.0001, 0.00375)  # per interval, decimals
INTERVALS = (0.5, 1.0, 4.0, 8.0, 24.0)  # hours

FORWARDS = (100.0, 65_000.0)
MONEYNESS = (0.3, 0.6, 0.8, 0.95, 1.0, 1.05, 1.25, 1.6, 3.0)  # K / F (or barrier / F), sorted ascending
DAYS = (1.0 / 24.0, 1.0, 7.0, 30.0, 90.0, 365.0, 730.0)  # 1 hour .. 2 years, sorted ascending
IVS = (0.05, 0.2, 0.5, 0.8, 1.5)  # sorted ascending

PROB_GRID = [(F, F * m, d / 365.0, s) for F, m, d, s in itertools.product(FORWARDS, MONEYNESS, DAYS, IVS)]
GRID_SIZE = len(PROB_GRID)

# Degenerate cases: where a formula is known to collapse, with the reason. Each has a pinning test below.
DEGENERATE = {
    "basis_near_expiry": (
        "days to expiry <= 0.05 (72 min): annualised basis is None (pct * 365 / days blows up); an expired contract "
        "reports days_to_expiry = 0 and keeps basis_abs / basis_pct"
    ),
    "prob_above_zero_t_or_vol": (
        "prob_above needs T > 0 and IV > 0 (d1d2 divides by sigma * sqrt(T), ZeroDivisionError); unreachable from "
        "tools: build_smile returns None for T <= 0 and smile IVs are positive (smoothed fit floored at 1e-4)"
    ),
    "prob_above_flat_wing_edge": (
        "Smile.iv is flat outside the quoted strikes, so at the first/last strike the IV slope jumps to 0 (a concave "
        "kink) and P(S > K) can rise across the edge (~1e-4 on a 30-day put skew); prob_from_options warns when the "
        "level is outside the quoted strikes"
    ),
    "prob_touch_zero_t_or_vol": ("prob_touch at T <= 0 or sigma <= 0 is 1 if barrier == F else 0 by convention (no path to touch on)"),
}

REL = 1e-12


def _report(fails: list[str], size: int = GRID_SIZE) -> str:
    return f"{len(fails)} of {size} grid points fail, first: " + "; ".join(fails[:5])


def _flat(F: float, T: float, s: float) -> Smile:
    return Smile(F, T, [F * m for m in (0.25, 0.5, 1.0, 2.0, 4.0)], [s] * 5)


def test_prob_grid_is_the_documented_size():
    assert GRID_SIZE == 630


# =====================================================================================================
# derivatives.py: funding
# =====================================================================================================
def test_annualize_funding_is_rate_times_periods_per_year():
    for rate, h in itertools.product(RATES, INTERVALS):
        assert annualize_funding(rate, h) == pytest.approx(rate * 8760.0 / h, rel=1e-15, abs=0.0)
    # the two cadences that matter: 8h x 1095 (Binance, Bybit, Deribit), 1h x 8760 (Hyperliquid)
    assert annualize_funding(0.0001, 8.0) == pytest.approx(0.1095, rel=1e-15)
    assert annualize_funding(0.0000125, 1.0) == pytest.approx(0.1095, rel=1e-15)


def test_annualize_funding_is_linear_odd_and_cadence_free():
    for h in INTERVALS:
        assert annualize_funding(0.0, h) == 0.0
        for rate in RATES:
            assert annualize_funding(-rate, h) == -annualize_funding(rate, h)
            assert annualize_funding(3.0 * rate, h) == pytest.approx(3.0 * annualize_funding(rate, h), rel=1e-15, abs=0.0)
            # the same hourly rate gives the same APR whatever the payment cadence
            assert annualize_funding(rate * h, h) == pytest.approx(annualize_funding(rate, 1.0), rel=1e-12, abs=1e-18)
        # monotone in the rate
        apr = [annualize_funding(r, h) for r in sorted(RATES)]
        assert all(b > a for a, b in itertools.pairwise(apr))


def test_parse_interval_hours_units():
    for text, hours in (("8h", 8.0), ("1h", 1.0), ("480m", 8.0), ("30m", 0.5), ("1d", 24.0), (" 4H ", 4.0)):
        assert parse_interval_hours(text) == hours
        assert annualize_funding(0.0001, parse_interval_hours(text)) == pytest.approx(0.0001 * 8760.0 / hours, rel=1e-15)
    assert parse_interval_hours(4) == 4.0
    for bad in ("", "8", "8w", "-8h", 0, -1.0, None):
        assert parse_interval_hours(bad) == 8.0
        assert parse_interval_hours(bad, default=1.0) == 1.0


def _series(rates: list[float], hours: float, start_ms: int = 1_760_000_000_000) -> list[dict]:
    step = int(hours * 3_600_000)
    return [{"ts_ms": start_ms + i * step, "rate": r} for i, r in enumerate(rates)]


def test_summary_constant_rate_carry_equals_annualised_rate():
    for rate, h in itertools.product(RATES, (1.0, 4.0, 8.0)):
        n = int(30 * 24 / h)
        s = summarise_funding_history(_series([rate] * n, h))
        assert s["interval_hours"] == h  # detected from the spacing
        apr = annualize_funding(rate, h)
        for key in ("carry_annualized", "latest_annualized"):
            assert s[key] == pytest.approx(apr, rel=1e-12, abs=1e-15), (rate, h, key)
        assert s["mean_rate"] == pytest.approx(rate, rel=1e-12, abs=1e-18)
        assert s["stdev_rate"] == pytest.approx(0.0, abs=1e-18)
        assert s["z_score_latest"] is None  # zero spread: no z-score
        assert s["percentile_latest"] == 0.5  # all ties
        assert s["polarity_flips"] == 0


def test_summary_carry_same_hourly_rate_across_cadence_switch():
    """8h payments of 8r followed by 1h payments of r: carry stays near r * 8760 (within the one-interval edge)."""
    r = 0.00001
    rows = _series([8 * r] * 30, 8.0) + _series([r] * 240, 1.0, start_ms=1_760_000_000_000 + 30 * 8 * 3_600_000)
    s = summarise_funding_history(rows)
    assert s["carry_annualized"] == pytest.approx(r * 8760.0, rel=0.02)
    assert s["latest_annualized"] == pytest.approx(r * 8760.0, rel=1e-12)


_MIXED = [0.0001, 0.00012, -0.00005, 0.0, 0.0003, -0.0002, 0.00008, 0.0001, -0.00001, 0.00015]


def test_summary_scale_and_shift_invariance():
    base = summarise_funding_history(_series(_MIXED, 8.0))
    for lam in (0.5, 3.0, 1000.0):
        sc = summarise_funding_history(_series([lam * x for x in _MIXED], 8.0))
        for key in ("mean_rate", "stdev_rate", "min_rate", "max_rate", "carry_annualized", "latest_annualized"):
            assert sc[key] == pytest.approx(lam * base[key], rel=1e-12), key
        for key in ("z_score_latest", "percentile_latest", "polarity_flips"):
            assert sc[key] == pytest.approx(base[key], rel=1e-12), key
    # a constant shift moves the level, not the dispersion or the latest value's z / rank
    sh = summarise_funding_history(_series([x + 0.001 for x in _MIXED], 8.0))
    assert sh["mean_rate"] == pytest.approx(base["mean_rate"] + 0.001, rel=1e-12)
    assert sh["stdev_rate"] == pytest.approx(base["stdev_rate"], rel=1e-9)
    assert sh["z_score_latest"] == pytest.approx(base["z_score_latest"], rel=1e-9)
    assert sh["percentile_latest"] == base["percentile_latest"]
    # negation flips the sign of every level and of the z-score, not the count of sign changes
    ng = summarise_funding_history(_series([-x for x in _MIXED], 8.0))
    for key in ("mean_rate", "carry_annualized", "latest_annualized", "z_score_latest"):
        assert ng[key] == pytest.approx(-base[key], rel=1e-12), key
    assert ng["polarity_flips"] == base["polarity_flips"]


def test_polarity_flips_count_sign_changes_only():
    """'polarity flips (sign changes)': a zero rate has no sign, so it neither starts nor ends a flip by itself."""
    cases = {
        (0.0, 0.0, 0.0, 0.0): 0,
        (0.0001, 0.0, 0.0001): 0,
        (-0.0001, 0.0, 0.0, -0.0002): 0,
        (0.0, 0.0001, 0.0002): 0,
        (0.0001, 0.0, -0.0001): 1,
        (0.0001, -0.0001, 0.0001, -0.0001): 3,
        (0.0001, 0.0, -0.0001, 0.0, 0.0001): 2,
    }
    for rates, flips in cases.items():
        assert summarise_funding_history(_series(list(rates), 8.0))["polarity_flips"] == flips, rates
    # a constant-sign series (zeros allowed) never flips; any series has at most n - 1 flips
    for rates in itertools.product((0.0, 0.0001, 0.0003), repeat=4):
        assert summarise_funding_history(_series(list(rates), 8.0))["polarity_flips"] == 0, rates
    for rates in itertools.product((-0.0001, 0.0, 0.0001), repeat=4):
        assert 0 <= summarise_funding_history(_series(list(rates), 8.0))["polarity_flips"] <= 3


# =====================================================================================================
# derivatives.py: basis
# =====================================================================================================
def _basis_grid():
    for idx, ratio, d in itertools.product(INDEXES, RATIOS, BASIS_DAYS):
        yield idx * ratio, idx, d, basis(idx * ratio, idx, NOW + timedelta(days=d), NOW)


def test_basis_definition_and_units():
    fails = []
    for mark, idx, d, b in _basis_grid():
        pct = mark / idx - 1.0
        ok = (
            b["days_to_expiry"] == round(d, 3)
            and b["basis_abs"] == mark - idx
            and b["basis_pct"] == pct
            and math.isclose(b["annualized_simple"], pct * 365.0 / d, rel_tol=1e-9, abs_tol=1e-15)
            and math.isclose(b["annualized_log"], math.log(mark / idx) * 365.0 / d, rel_tol=1e-9, abs_tol=1e-15)
        )
        if not ok:
            fails.append(f"mark={mark} index={idx} days={d}: {b}")
    assert not fails, _report(fails, len(INDEXES) * len(RATIOS) * len(BASIS_DAYS))
    # 1 % over 365 days is 1 % a year; 1 % over 36.5 days is 10 % a year
    assert basis(101.0, 100.0, NOW + timedelta(days=365), NOW)["annualized_simple"] == pytest.approx(0.01, rel=1e-12)
    assert basis(101.0, 100.0, NOW + timedelta(days=36.5), NOW)["annualized_simple"] == pytest.approx(0.10, rel=1e-12)


def test_basis_sign_and_zero():
    for mark, idx, d, b in _basis_grid():
        sign = (mark > idx) - (mark < idx)  # contango (future above spot) -> positive
        for key in ("basis_abs", "basis_pct", "annualized_simple", "annualized_log"):
            v = b[key]
            assert (v > 0) - (v < 0) == sign, (mark, idx, d, key, v)
        if mark == idx:
            assert b["basis_abs"] == b["basis_pct"] == b["annualized_simple"] == b["annualized_log"] == 0.0
        # ln(1 + x) <= x: log annualisation never exceeds the simple one
        assert b["annualized_log"] <= b["annualized_simple"] + 1e-15


def test_basis_monotone_in_mark_index_and_days():
    for idx, d in itertools.product(INDEXES, BASIS_DAYS):
        exp = NOW + timedelta(days=d)
        rows = [basis(idx * r, idx, exp, NOW) for r in RATIOS]
        for key in ("basis_abs", "basis_pct", "annualized_simple", "annualized_log"):
            assert all(b[key] > a[key] for a, b in itertools.pairwise(rows)), (idx, d, key)
        # higher index, same mark -> lower basis
        by_index = [basis(idx, idx * r, exp, NOW)["basis_pct"] for r in RATIOS]
        assert all(b < a for a, b in itertools.pairwise(by_index))
    # same basis_pct spread over more days -> smaller annualised magnitude
    for ratio in RATIOS:
        ann = [basis(100.0 * ratio, 100.0, NOW + timedelta(days=d), NOW)["annualized_simple"] for d in BASIS_DAYS]
        assert all(abs(b) <= abs(a) for a, b in itertools.pairwise(ann)), (ratio, ann)


def test_basis_scale_invariance():
    lam = 650.0
    for mark, idx, d, b in _basis_grid():
        if idx != INDEXES[0]:
            continue
        s = basis(mark * lam, idx * lam, NOW + timedelta(days=d), NOW)
        assert s["basis_abs"] == pytest.approx(lam * b["basis_abs"], rel=1e-9, abs=1e-9)
        for key in ("basis_pct", "annualized_simple", "annualized_log"):
            assert s[key] == pytest.approx(b[key], rel=1e-9, abs=1e-13), (mark, d, key)


# =====================================================================================================
# probability.py: prob_above (Breeden-Litzenberger on the smile)
# =====================================================================================================
def _step(K: float, T: float, s: float) -> float:
    # strike step scaled to the distribution width K*sigma*sqrt(T), else truncation dominates at 1 hour / low vol
    return 1e-3 * K * min(1.0, s * math.sqrt(T))


def _digital_call_bl(F: float, K: float, T: float, iv_of, h: float) -> float:
    """-dC/dK by a central difference of Black-76 prices with the smile IV at each strike."""
    return -(black76_price(F, K + h, T, iv_of(K + h), True) - black76_price(F, K - h, T, iv_of(K - h), True)) / (2 * h)


def _digital_put_bl(F: float, K: float, T: float, iv_of, h: float) -> float:
    """dP/dK = P(S_T < K) from the put side."""
    return (black76_price(F, K + h, T, iv_of(K + h), False) - black76_price(F, K - h, T, iv_of(K - h), False)) / (2 * h)


def test_prob_above_flat_smile_is_nd2_and_black76_digital():
    fails = []
    for F, K, T, s in PROB_GRID:
        p = prob_above(_flat(F, T, s), K)
        nd2 = ncdf(d1d2(F, K, T, s)[1])
        h = _step(K, T, s)
        call_dig = _digital_call_bl(F, K, T, lambda _k, v=s: v, h)
        put_dig = _digital_put_bl(F, K, T, lambda _k, v=s: v, h)
        if not (0.0 <= p <= 1.0) or abs(p - nd2) > 1e-12:
            fails.append(f"N(d2) F={F} K={K} T={T} s={s}: {p} vs {nd2}")
        # finite-difference noise ~1e-16 * F / h <= 2e-10, truncation ~1e-6 relative: 1e-6 absolute covers both
        if abs(p - call_dig) > 1e-6 or abs((1.0 - p) - put_dig) > 1e-6:
            fails.append(f"digital F={F} K={K} T={T} s={s}: {p} vs call {call_dig}, 1-p vs put {put_dig}")
    assert not fails, _report(fails)


def test_prob_above_depends_on_total_vol_only_and_is_scale_free():
    fails = []
    for F, K, T, s in PROB_GRID:
        p = prob_above(_flat(F, T, s), K)
        if abs(prob_above(_flat(F, T / 4.0, 2.0 * s), K) - p) > 1e-12:
            fails.append(f"sigma*sqrt(T) F={F} K={K} T={T} s={s}")
        if abs(prob_above(_flat(F * 7.0, T, s), K * 7.0) - p) > 1e-12:
            fails.append(f"scale F={F} K={K} T={T} s={s}")
    assert not fails, _report(fails)


def test_prob_above_non_increasing_in_strike_flat():
    for F, d, s in itertools.product(FORWARDS, DAYS, IVS):
        sm = _flat(F, d / 365.0, s)
        ps = [prob_above(sm, F * m) for m in MONEYNESS]
        assert all(b <= a for a, b in itertools.pairwise(ps)), (F, d, s, ps)
        # at the forward the driftless log-normal puts slightly less than half the mass above F
        assert ps[MONEYNESS.index(1.0)] == pytest.approx(ncdf(-0.5 * s * math.sqrt(d / 365.0)), abs=1e-12)
        assert ps[MONEYNESS.index(1.0)] < 0.5


# A realistic skewed, convex BTC-style smile (put wing richer), quoted on 13 strikes.
_SKEW_M = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2, 1.4, 1.6)


def _skew_smile(F: float, T: float) -> Smile:
    ivs = [0.5 - 0.12 * math.log(m) + 0.3 * math.log(m) ** 2 for m in _SKEW_M]
    return Smile(F, T, [F * m for m in _SKEW_M], ivs)


def test_prob_above_skewed_smile_matches_breeden_litzenberger():
    """Off the interpolation nodes the slope term equals the exact -dC/dK of Black-76 priced on the smile, and
    P(S > K) + P(S < K) = 1 with P(S < K) = dP/dK from the put side (the tool's 'below' is 1 - above).

    Within 0.5 % of a node prob_above's own slope step (h = 0.005) averages across the kink of the piecewise-linear
    smile, which has no derivative there; at 1 day this differs from the one-sided value by ~2e-4.
    """
    fails = []
    days = (1.0, 7.0, 30.0, 90.0, 365.0)
    for F, d in itertools.product(FORWARDS, days):
        T = d / 365.0
        sm = _skew_smile(F, T)
        for a, b in itertools.pairwise(_SKEW_M):
            K = F * (a + b) / 2.0  # mid-segment: more than 0.5 % from a node, the smile is linear in log K there
            p = prob_above(sm, K)
            h = _step(K, T, sm.iv(K))
            bl, put_dig = _digital_call_bl(F, K, T, sm.iv, h), _digital_put_bl(F, K, T, sm.iv, h)
            if not (0.0 <= p <= 1.0) or abs(p - bl) > 1e-4 or abs(p + put_dig - 1.0) > 1e-4:
                fails.append(f"F={F} T={T} K={K}: {p} vs BL {bl}, below {put_dig}")
            if p in (0.0, 1.0):
                continue  # saturated far out at 1 day: no slope sign to check
            # the slope term has the sign of -dIV/dK: a falling smile (put skew) adds probability above, a rising
            # call wing removes it
            slope = sm.iv(F * b) - sm.iv(F * a)
            excess = p - ncdf(d1d2(F, K, T, sm.iv(K))[1])
            if (excess > 0) - (excess < 0) != (slope < 0) - (slope > 0):
                fails.append(f"skew sign F={F} T={T} K={K}: excess {excess}, IV slope {slope}")
    assert not fails, _report(fails, len(FORWARDS) * len(days) * (len(_SKEW_M) - 1))


def test_prob_above_skewed_smile_monotone_inside_quoted_range():
    """Monotone on a fine strike grid crossing every interior node (convex smile: no butterfly arbitrage);
    the edges of the quoted range are DEGENERATE['prob_above_flat_wing_edge']."""
    lo, hi = _SKEW_M[0] * 1.005, _SKEW_M[-1] * 0.995  # outside the slope finite difference's reach of the edge
    for F, d in itertools.product(FORWARDS, (1.0, 7.0, 30.0, 90.0, 365.0)):
        T = d / 365.0
        sm = _skew_smile(F, T)
        ks = [F * (lo + (hi - lo) * i / 600) for i in range(601)]
        ps = [prob_above(sm, k) for k in ks]
        assert all(0.0 <= p <= 1.0 for p in ps), (F, d)  # exactly 0 / 1 only far out at 1 day (ncdf saturates)
        assert all(b <= a + 1e-12 for a, b in itertools.pairwise(ps)), (F, d)


def test_prob_above_limits_short_t_and_low_vol():
    for F in FORWARDS:
        for T, s in ((1e-12, 0.5), (30 / 365.0, 1e-9)):
            sm = _flat(F, T, s)
            assert prob_above(sm, 0.99 * F) == 1.0
            assert prob_above(sm, 1.01 * F) == 0.0
            assert prob_above(sm, F) == pytest.approx(0.5, abs=1e-6)


# =====================================================================================================
# probability.py: prob_touch (continuous barrier, driftless forward)
# =====================================================================================================
def test_prob_touch_bounds_and_terminal_parity_with_black76():
    """Reflection on a driftless log-normal forward, exact in Black-76 terms:

    up   (B > F): P_touch = 2 N(d2) + C(K=B) / B
    down (B < F): P_touch = 2 N(-d2) - P(K=B) / B
    so the touch probability is at least the terminal one (and for an up barrier at least twice it).
    """
    fails = []
    for F, B, T, s in PROB_GRID:
        p = prob_touch(F, B, s, T)
        if not (0.0 <= p <= 1.0):
            fails.append(f"range F={F} B={B} T={T} s={s}: {p}")
            continue
        if B == F:
            if p != 1.0:
                fails.append(f"at F F={F} T={T} s={s}: {p}")
            continue
        d2 = d1d2(F, B, T, s)[1]
        if B > F:
            ref, terminal = 2 * ncdf(d2) + black76_price(F, B, T, s, True) / B, ncdf(d2)
            if p < 2 * terminal - 1e-15:
                fails.append(f"2x terminal F={F} B={B} T={T} s={s}: {p} vs {terminal}")
        else:
            ref, terminal = 2 * ncdf(-d2) - black76_price(F, B, T, s, False) / B, ncdf(-d2)
        if abs(p - ref) > 1e-12 or p < terminal - 1e-15:
            fails.append(f"parity F={F} B={B} T={T} s={s}: {p} vs {ref}, terminal {terminal}")
    assert not fails, _report(fails)


def test_prob_touch_depends_on_total_vol_and_is_scale_free():
    fails = []
    for F, B, T, s in PROB_GRID:
        p = prob_touch(F, B, s, T)
        if abs(prob_touch(F, B, 2.0 * s, T / 4.0) - p) > 1e-12:
            fails.append(f"sigma*sqrt(T) F={F} B={B} T={T} s={s}")
        if abs(prob_touch(7.0 * F, 7.0 * B, s, T) - p) > 1e-12:
            fails.append(f"scale F={F} B={B} T={T} s={s}")
    assert not fails, _report(fails)


def test_prob_touch_monotone_in_time_vol_and_distance():
    for F, m, s in itertools.product(FORWARDS, MONEYNESS, IVS):
        by_t = [prob_touch(F, F * m, s, d / 365.0) for d in DAYS]
        assert all(b >= a - 1e-15 for a, b in itertools.pairwise(by_t)), (F, m, s, by_t)
    for F, m, d in itertools.product(FORWARDS, MONEYNESS, DAYS):
        by_s = [prob_touch(F, F * m, s, d / 365.0) for s in IVS]
        assert all(b >= a - 1e-15 for a, b in itertools.pairwise(by_s)), (F, m, d, by_s)
    # farther barriers are touched less often, on each side of F
    up = [m for m in MONEYNESS if m >= 1.0]
    down = [m for m in reversed(MONEYNESS) if m <= 1.0]
    for F, d, s in itertools.product(FORWARDS, DAYS, IVS):
        for side in (up, down):
            ps = [prob_touch(F, F * m, s, d / 365.0) for m in side]
            assert all(b <= a + 1e-15 for a, b in itertools.pairwise(ps)), (F, d, s, side, ps)


def test_prob_touch_limits():
    F = 65_000.0
    for B in (0.9 * F, 1.1 * F):
        assert prob_touch(F, B, 0.5, 1e-12) == 0.0  # T -> 0
        assert prob_touch(F, B, 1e-9, 1.0) == 0.0  # sigma -> 0
    # T -> infinity: a martingale reaches B > F with probability F / B; the down barrier is reached surely
    assert prob_touch(F, 1.3 * F, 0.5, 1e6) == pytest.approx(1 / 1.3, rel=1e-9)
    assert prob_touch(F, 0.7 * F, 0.5, 1e6) == pytest.approx(1.0, abs=1e-9)


# ---------- degenerate cases (DEGENERATE) ----------
def test_degenerate_basis_near_expiry():
    assert "basis_near_expiry" in DEGENERATE
    b = basis(101.0, 100.0, NOW + timedelta(days=0.05), NOW)
    assert b["annualized_simple"] is None and b["annualized_log"] is None
    assert b["basis_pct"] == pytest.approx(0.01) and b["basis_abs"] == pytest.approx(1.0)
    assert basis(101.0, 100.0, NOW + timedelta(days=0.051), NOW)["annualized_simple"] == pytest.approx(0.01 * 365 / 0.051)
    expired = basis(101.0, 100.0, NOW - timedelta(days=1), NOW)
    assert expired["days_to_expiry"] == 0.0 and expired["annualized_simple"] is None


def test_degenerate_prob_above_zero_t_or_vol():
    assert "prob_above_zero_t_or_vol" in DEGENERATE
    for T, s in ((0.0, 0.5), (0.1, 0.0)):
        with pytest.raises(ZeroDivisionError):
            prob_above(_flat(100.0, T, s), 110.0)


def test_degenerate_prob_above_flat_wing_edge():
    assert "prob_above_flat_wing_edge" in DEGENERATE
    F, T = 65_000.0, 30 / 365.0
    sm = _skew_smile(F, T)
    outside, inside = prob_above(sm, 0.4975 * F), prob_above(sm, 0.5025 * F)  # across the first quoted strike 0.5 F
    assert not sm.covers(0.4975 * F) and sm.covers(0.5025 * F)
    assert 0.0 < inside - outside < 1e-3  # rises with K: the flat extrapolation, not the interior smile


def test_degenerate_prob_touch_zero_t_or_vol():
    assert "prob_touch_zero_t_or_vol" in DEGENERATE
    for T, s in ((0.0, 0.5), (-1.0, 0.5), (0.1, 0.0)):
        assert prob_touch(100.0, 100.0, s, T) == 1.0
        assert prob_touch(100.0, 110.0, s, T) == 0.0
        assert prob_touch(100.0, 90.0, s, T) == 0.0
