"""Mathematical invariants of the Black-76 functions on a deterministic grid (no network, no hypothesis).

Conventions under test (docs/ARCHITECTURE.md, analytics/options.py): Black-76 with r = 0 on the expiry
forward F, T = days / 365, IV as a decimal, vega in USD per 1 vol point (0.01), theta in USD per
calendar day, both per 1 unit of underlying. A silent change of a sign, of the /365 or of the vega
multiplier breaks at least one test here before it reaches the smile, GEX or the forecast logs.

Known degenerate cases are not hidden by tolerances: they are listed in ``DEGENERATE`` with a reason
and pinned by their own tests.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

import pytest

from tradedesk.analytics.options import black76_greeks, black76_price, year_fraction
from tradedesk.analytics.put_skew import implied_vol_inverse

FORWARDS = (100.0, 65_000.0)
MONEYNESS = (0.3, 0.6, 0.8, 0.95, 1.0, 1.05, 1.25, 1.6, 3.0)  # K / F: deep ITM/OTM on both sides
DAYS = (1.0 / 24.0, 1.0, 7.0, 30.0, 90.0, 365.0, 730.0)  # 1 hour .. 2 years
IVS = (0.05, 0.2, 0.5, 0.8, 1.5)  # sorted ascending (monotonicity tests rely on it)

GRID = [(F, F * m, d / 365.0, s) for F, m, d, s in itertools.product(FORWARDS, MONEYNESS, DAYS, IVS)]
GRID_SIZE = len(FORWARDS) * len(MONEYNESS) * len(DAYS) * len(IVS)

# Degenerate cases: where the formula is known to collapse, with the reason. Each has a pinning test below.
DEGENERATE = {
    "expiry_or_zero_vol": (
        "T <= 0 or sigma <= 0: price is the intrinsic value and every greek is 0 by convention, "
        "including delta of an ITM option (the true limit is 1 / -1); year_fraction clips expired options to T = 0"
    ),
    "iv_unrecoverable": (
        "implied vol is not identifiable when a 1e-6 vol bump moves the price by less than 1e-12 of the price "
        "(~1e4 ulps: the time value is lost in float rounding, deep ITM/OTM at short T and low IV) "
        "or the price underflows to 0"
    ),
}

REL = 1e-9  # float tolerance for identities, relative to the forward scale


def _call_put(F: float, K: float, T: float, s: float) -> tuple[float, float]:
    return black76_price(F, K, T, s, True), black76_price(F, K, T, s, False)


def _report(fails: list[str]) -> str:
    return f"{len(fails)} of {GRID_SIZE} grid points fail, first: " + "; ".join(fails[:5])


def test_grid_is_the_documented_size():
    assert GRID_SIZE == 630
    assert len(GRID) == GRID_SIZE


# ---------- prices ----------
def test_put_call_parity_r0():
    fails = []
    for F, K, T, s in GRID:
        c, p = _call_put(F, K, T, s)
        if abs((c - p) - (F - K)) > REL * max(F, K):
            fails.append(f"F={F} K={K} T={T} s={s}: C-P={c - p} F-K={F - K}")
    assert not fails, _report(fails)


def test_price_bounds():
    fails = []
    for F, K, T, s in GRID:
        c, p = _call_put(F, K, T, s)
        tol = REL * max(F, K)
        if not (max(F - K, 0.0) - tol <= c <= F + tol):
            fails.append(f"call F={F} K={K} T={T} s={s}: {c}")
        if not (max(K - F, 0.0) - tol <= p <= K + tol):
            fails.append(f"put F={F} K={K} T={T} s={s}: {p}")
    assert not fails, _report(fails)


def test_price_non_decreasing_in_iv():
    fails = []
    for F, m, d in itertools.product(FORWARDS, MONEYNESS, DAYS):
        K, T = F * m, d / 365.0
        for is_call in (True, False):
            prices = [black76_price(F, K, T, s, is_call) for s in IVS]
            if any(b < a - REL * F for a, b in itertools.pairwise(prices)):
                fails.append(f"F={F} K={K} T={T} call={is_call}: {prices}")
    assert not fails, _report(fails)


def test_price_non_decreasing_in_t_from_intrinsic():
    """r = 0: more time never makes an option cheaper; T = 0 is the intrinsic value."""
    fails = []
    for F, m, s in itertools.product(FORWARDS, MONEYNESS, IVS):
        K = F * m
        for is_call in (True, False):
            prices = [black76_price(F, K, d / 365.0, s, is_call) for d in (0.0, *DAYS)]
            if any(b < a - REL * F for a, b in itertools.pairwise(prices)):
                fails.append(f"F={F} K={K} s={s} call={is_call}: {prices}")
    assert not fails, _report(fails)


def test_price_strictly_increasing_atm():
    """At the money the time value is never lost in rounding: strict growth in IV and in T."""
    for F in FORWARDS:
        by_iv = [black76_price(F, F, 30 / 365.0, s, True) for s in IVS]
        by_t = [black76_price(F, F, d / 365.0, 0.5, True) for d in DAYS]
        assert all(b > a for a, b in itertools.pairwise(by_iv))
        assert all(b > a for a, b in itertools.pairwise(by_t))


def test_price_homogeneous_degree_one():
    """Scaling F and K by lambda scales the price by lambda and leaves delta unchanged."""
    lam = 650.0
    fails = []
    for F, K, T, s in GRID:
        if F != FORWARDS[0]:
            continue
        for is_call in (True, False):
            a = black76_price(F, K, T, s, is_call) * lam
            b = black76_price(F * lam, K * lam, T, s, is_call)
            da = black76_greeks(F, K, T, s, is_call)["delta"]
            db = black76_greeks(F * lam, K * lam, T, s, is_call)["delta"]
            if abs(a - b) > REL * F * lam or abs(da - db) > 1e-12:
                fails.append(f"K={K} T={T} s={s} call={is_call}: {a} vs {b}, delta {da} vs {db}")
    assert not fails, _report(fails)


# ---------- greeks: signs and call/put relations ----------
def test_delta_ranges_and_call_minus_put_is_one():
    fails = []
    for F, K, T, s in GRID:
        gc, gp = black76_greeks(F, K, T, s, True), black76_greeks(F, K, T, s, False)
        if not (0.0 <= gc["delta"] <= 1.0 and -1.0 <= gp["delta"] <= 0.0):
            fails.append(f"range F={F} K={K} T={T} s={s}: {gc['delta']} {gp['delta']}")
        if abs(gc["delta"] - gp["delta"] - 1.0) > 1e-12:
            fails.append(f"parity F={F} K={K} T={T} s={s}: {gc['delta'] - gp['delta']}")
    assert not fails, _report(fails)


def test_delta_non_decreasing_in_forward():
    for m, d, s in itertools.product(MONEYNESS, DAYS, IVS):
        K, T = 100.0 * m, d / 365.0
        deltas = [black76_greeks(F, K, T, s, True)["delta"] for F in (50.0, 80.0, 95.0, 100.0, 105.0, 125.0, 200.0)]
        assert all(b >= a for a, b in itertools.pairwise(deltas)), (K, T, s, deltas)


def test_gamma_vega_theta_signs_and_call_put_equality():
    fails = []
    for F, K, T, s in GRID:
        gc, gp = black76_greeks(F, K, T, s, True), black76_greeks(F, K, T, s, False)
        if gc["gamma"] < 0 or gc["vega"] < 0 or gc["theta"] > 0:
            fails.append(f"sign F={F} K={K} T={T} s={s}: {gc}")
        if any(gc[k] != gp[k] for k in ("gamma", "vega", "theta")):
            fails.append(f"call != put F={F} K={K} T={T} s={s}: {gc} {gp}")
    assert not fails, _report(fails)


def test_greek_identities_fix_the_units():
    """Black-76, r = 0, per-year greeks: vega_y = sigma*T*F^2*gamma, theta_y = -sigma^2*F^2*gamma/2.

    With vega per 1 vol point (/100) and theta per calendar day (/365) this becomes
    vega = sigma*T*F^2*gamma/100 and theta = -sigma^2*F^2*gamma/(2*365): a change of either
    divisor (or of T = days/365) breaks these identities.
    """
    fails = []
    for F, K, T, s in GRID:
        g = black76_greeks(F, K, T, s, True)
        f2g = F * F * g["gamma"]
        if not math.isclose(g["vega"], s * T * f2g / 100.0, rel_tol=1e-9, abs_tol=1e-300):
            fails.append(f"vega F={F} K={K} T={T} s={s}: {g['vega']} vs {s * T * f2g / 100.0}")
        if not math.isclose(g["theta"], -s * s * f2g / 730.0, rel_tol=1e-9, abs_tol=1e-300):
            fails.append(f"theta F={F} K={K} T={T} s={s}: {g['theta']} vs {-s * s * f2g / 730.0}")
    assert not fails, _report(fails)


def test_greeks_match_finite_differences_in_documented_units():
    """delta = dC/dF, gamma = d(delta)/dF, vega = dC/d(sigma) * 0.01, theta = -dC/dT / 365 (central differences)."""
    fails = []
    for F, K, T, s in GRID:
        for is_call in (True, False):
            g = black76_greeks(F, K, T, s, is_call)

            def px(f=F, t=T, v=s, k=K, c=is_call):
                return black76_price(f, k, t, v, c)

            # F step scaled to the distribution width F*sigma*sqrt(T), else truncation dominates at 1 hour
            hf, hs, ht = 1e-3 * F * s * math.sqrt(T), 1e-5, 1e-4 * T
            fd = {
                "delta": (px(f=F + hf) - px(f=F - hf)) / (2 * hf),
                "gamma": (black76_greeks(F + hf, K, T, s, is_call)["delta"] - black76_greeks(F - hf, K, T, s, is_call)["delta"]) / (2 * hf),
                "vega": (px(v=s + hs) - px(v=s - hs)) / (2 * hs) * 0.01,
                "theta": -(px(t=T + ht) - px(t=T - ht)) / (2 * ht) / 365.0,
            }
            # absolute floors: the price noise of a central difference is ~1e-16*F/h
            floor = {"delta": 1e-7, "gamma": 1e-7 / F, "vega": 1e-9 * F, "theta": 1e-9 * F / (T * 365.0)}
            for k, v in fd.items():
                if not math.isclose(g[k], v, rel_tol=1e-4, abs_tol=floor[k]):
                    fails.append(f"{k} F={F} K={K} T={T} s={s} call={is_call}: {g[k]} vs fd {v}")
    assert not fails, _report(fails)


def test_year_fraction_is_days_over_365():
    now = datetime(2026, 11, 6, 8, tzinfo=UTC)
    assert year_fraction(now + timedelta(days=365), now) == 1.0
    assert year_fraction(now + timedelta(days=1), now) == pytest.approx(1.0 / 365.0, rel=1e-15)
    assert year_fraction(now - timedelta(hours=1), now) == 0.0  # expired: clipped, see DEGENERATE
    # one calendar day of decay is about theta (exact as the step goes to 0)
    F, K, s = 65_000.0, 65_000.0, 0.5
    T = year_fraction(now + timedelta(days=30), now)
    one_day = black76_price(F, K, T - 1.0 / 365.0, s, True) - black76_price(F, K, T, s, True)
    assert one_day == pytest.approx(black76_greeks(F, K, T, s, True)["theta"], rel=0.02)


# ---------- implied vol round trip ----------
def _iv_unrecoverable(F: float, K: float, T: float, s: float, is_call: bool) -> bool:
    price = black76_price(F, K, T, s, is_call)
    vega_raw = black76_greeks(F, K, T, s, is_call)["vega"] * 100.0
    return price <= 0.0 or vega_raw * 1e-6 < 1e-12 * price


def test_implied_vol_round_trip():
    fails, skipped = [], []
    for F, K, T, s in GRID:
        for is_call in (True, False):
            if _iv_unrecoverable(F, K, T, s, is_call):
                skipped.append((F, K, T, s, is_call))
                continue
            back = implied_vol_inverse(black76_price(F, K, T, s, is_call) / F, F, K, T, is_call)
            if back is None or abs(back - s) > 1e-6:
                fails.append(f"F={F} K={K} T={T} s={s} call={is_call}: {back}")
    assert not fails, _report(fails)
    # the exception is narrow: every strike within 3 standard deviations of F is recovered, and most of the grid
    assert not [p for p in skipped if abs(math.log(p[1] / p[0])) <= 3 * p[3] * math.sqrt(p[2])], skipped
    # 340 of 1260 on this grid (96 zero prices, the rest deep ITM at short T / low IV); the observed failure
    # boundary of the solver is a ratio ~1e-15, three orders below the 1e-12 cut-off
    assert len(skipped) < 0.3 * 2 * GRID_SIZE, len(skipped)


def test_implied_vol_round_trip_deep_wings_and_short_t():
    """Points the task names explicitly: deep ITM/OTM and 1 hour to expiry, where the vol is still identifiable."""
    F = 65_000.0
    for K, T, s, is_call in (
        (0.6 * F, 7 / 365, 0.8, False),  # deep OTM put
        (0.6 * F, 7 / 365, 0.8, True),  # deep ITM call
        (1.6 * F, 30 / 365, 0.5, True),  # deep OTM call
        (1.6 * F, 30 / 365, 0.5, False),  # deep ITM put
        (F, 1 / (24 * 365), 0.5, True),  # ATM, 1 hour
        (1.05 * F, 1 / (24 * 365), 1.5, True),  # near OTM, 1 hour, high vol
    ):
        assert not _iv_unrecoverable(F, K, T, s, is_call)
        back = implied_vol_inverse(black76_price(F, K, T, s, is_call) / F, F, K, T, is_call)
        assert back == pytest.approx(s, abs=1e-6), (K, T, s, is_call)


# ---------- degenerate cases (DEGENERATE) ----------
def test_degenerate_expiry_or_zero_vol_convention():
    assert "expiry_or_zero_vol" in DEGENERATE
    for T, s in ((0.0, 0.5), (-0.1, 0.5), (0.1, 0.0)):
        assert black76_price(100.0, 90.0, T, s, True) == 10.0
        assert black76_price(100.0, 90.0, T, s, False) == 0.0
        assert black76_price(100.0, 110.0, T, s, False) == 10.0
        # every greek is 0, even delta of the ITM call (true limit 1): pinned, not a silent change
        assert black76_greeks(100.0, 90.0, T, s, True) == {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    # just before expiry the ITM call delta is ~1: the jump to 0 at T = 0 is the convention above
    assert black76_greeks(100.0, 90.0, 1e-9, 0.5, True)["delta"] == pytest.approx(1.0)


def test_degenerate_iv_unrecoverable_examples():
    assert "iv_unrecoverable" in DEGENERATE
    F = 65_000.0
    # deep OTM call, 1 hour, low vol: the price underflows to 0, nothing to invert
    assert black76_price(F, 3 * F, 1 / (24 * 365), 0.05, True) == 0.0
    assert _iv_unrecoverable(F, 3 * F, 1 / (24 * 365), 0.05, True)
    assert implied_vol_inverse(0.0, F, 3 * F, 1 / (24 * 365), True) is None
    # deep ITM put, 1 hour, low vol: price == intrinsic to the last bit, the vol is not identifiable
    assert black76_price(F, 3 * F, 1 / (24 * 365), 0.05, False) == pytest.approx(2 * F, rel=1e-15)
    assert _iv_unrecoverable(F, 3 * F, 1 / (24 * 365), 0.05, False)
