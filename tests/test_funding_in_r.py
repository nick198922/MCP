import math

import numpy as np
import pytest

from tradedesk.analytics import funding_in_r as fr

H = fr.HOUR_MS
T0 = 1_700_000_000_000 - 1_700_000_000_000 % (8 * H)  # an 8 h settlement boundary


def series(n: int = 12, step_h: int = 8, rate: float = 0.0001, jitter: int = 7) -> tuple[np.ndarray, np.ndarray]:
    t = np.array([T0 + k * step_h * H + jitter for k in range(n)], dtype=np.int64)
    return t, np.full(n, rate)


def test_strict_interval_and_jitter() -> None:
    t, r = series()
    # entry exactly at a settlement (jittered by ms): not charged; exit exactly at one: not charged
    w = fr.window(t, r, T0 + 8 * H, T0 + 32 * H)
    assert w.n == 2 and w.total == pytest.approx(0.0002) and w.covered
    w2 = fr.window(t, r, T0 + 8 * H, T0 + 32 * H, inclusive=True)
    assert w2.n == 4 and w2.total == pytest.approx(0.0004)


def test_hold_without_settlement_is_zero_and_covered() -> None:
    t, r = series()
    w = fr.window(t, r, T0 + 9 * H, T0 + 15 * H)
    assert w.n == 0 and w.total == 0.0 and w.covered


def test_coverage_needs_span_and_no_holes() -> None:
    t, r = series()
    assert not fr.window(t, r, T0 - H, T0 + 20 * H).covered  # before the first settlement
    assert not fr.window(t, r, T0 + 80 * H, T0 + 100 * H).covered  # past the last one (T0 + 88 h)
    hole = np.delete(np.arange(t.size), 3)  # drop the settlement at T0 + 24 h: a 16 h gap
    assert not fr.window(t[hole], r[hole], T0 + 9 * H, T0 + 40 * H).covered
    assert fr.window(t[hole], r[hole], T0 + 33 * H, T0 + 60 * H).covered  # hole outside the hold
    assert not fr.window(np.array([], dtype=np.int64), np.array([]), T0, T0 + H).covered


def test_four_hour_interval_counts_every_settlement() -> None:
    t, r = series(n=30, step_h=4, rate=0.00005)
    w = fr.window(t, r, T0 + 1 * H, T0 + 25 * H)  # settlements at 4, 8, ..., 24 h
    assert w.n == 6 and w.total == pytest.approx(0.0003) and w.covered


def test_funding_r_sign_and_units() -> None:
    # long pays positive funding: 3 settlements of 0.01 % with a 2 % stop = -0.015 R
    assert fr.funding_r(0.0003, "long", 0.02) == pytest.approx(-0.015)
    assert fr.funding_r(0.0003, "short", 0.02) == pytest.approx(0.015)
    assert fr.funding_r(-0.0003, "short", 0.02) == pytest.approx(-0.015)
    assert math.isnan(fr.funding_r(0.0003, "long", 0.0))
    with pytest.raises(ValueError):
        fr.funding_r(0.0003, "buy", 0.02)


def test_passport_unit_is_smaller() -> None:
    # long: 0.02 + 0.00055 * (2 - 0.02) = 0.021089; short: 0.02 + 0.00055 * 2.02 = 0.021111
    assert fr.risk_frac(0.02, "long", 0.00055) == pytest.approx(0.021089)
    assert fr.risk_frac(0.02, "short", 0.00055) == pytest.approx(0.021111)
    v = fr.funding_r_passport_unit(0.0003, "long", 0.02, 0.00055)
    assert v == pytest.approx(-0.0003 / 0.021089)
    assert abs(v) < abs(fr.funding_r(0.0003, "long", 0.02))


def test_no_lookahead_past_exit() -> None:
    t, r = series(n=20)
    w = fr.window(t, r, T0 + 9 * H, T0 + 40 * H)
    r2 = r.copy()
    r2[t > T0 + 40 * H + H] = 0.05  # change everything after the first settlement at or after exit
    t2 = t.copy()
    t2[7:] += 3 * H  # and move later settlements (still covered: first after exit is T0 + 40 h)
    w2 = fr.window(t2, r2, T0 + 9 * H, T0 + 40 * H)
    assert (w2.total, w2.n, w2.covered) == (w.total, w.n, w.covered)


def test_summarize_and_rule() -> None:
    s = fr.summarize([-0.03, -0.01, 0.0, 0.01, math.nan])
    assert s["n"] == 4 and s["m"] == pytest.approx(-0.0075) and s["paid"] == pytest.approx(0.5)
    assert s["p5"] == pytest.approx(-0.027) and s["p95"] == pytest.approx(0.0085)
    assert math.isnan(fr.summarize([])["m"])
    assert fr.material(-0.02) and fr.material(0.025) and not fr.material(0.0199) and not fr.material(math.nan)
