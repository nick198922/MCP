"""Tests for analytics.condor_ivrv (playbook P1 condition: ATM IV / RV30, DVOL - RV30)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.condor_ivrv import (
    atm_iv_forward,
    condition_met,
    dvol_known_at,
    iv_rv_ratio,
    rv_known_at,
)
from tradedesk.analytics.trend_spread import DAY_MS

D0 = 1_700_000_000_000 // DAY_MS * DAY_MS


def _alternating(n: int, a: float) -> list[float]:
    """n closes whose log returns alternate +a, -a."""
    out = [100.0]
    for i in range(n - 1):
        out.append(out[-1] * math.exp(a if i % 2 == 0 else -a))
    return out


def test_rv_analytic_alternating_returns() -> None:
    a = 0.02
    closes = _alternating(31, a)  # 30 returns, mean 0, sum of squares 30 a^2
    stamps = [D0 + i * DAY_MS for i in range(31)]
    rv = rv_known_at(stamps, closes, stamps[-1] + DAY_MS)
    assert rv == pytest.approx(a * math.sqrt(30 / 29 * 365), rel=1e-9)


def test_rv_ignores_unclosed_bar_and_needs_31_closes() -> None:
    closes = _alternating(31, 0.02)
    stamps = [D0 + i * DAY_MS for i in range(31)]
    # at the open of the last bar it is not closed yet: only 30 closes known -> None
    assert rv_known_at(stamps, closes, stamps[-1]) is None
    assert rv_known_at(stamps, closes, stamps[-1] + DAY_MS) is not None


def test_rv_no_lookahead() -> None:
    closes = _alternating(60, 0.01)
    stamps = [D0 + i * DAY_MS for i in range(60)]
    t = stamps[40] + DAY_MS
    before = rv_known_at(stamps, closes, t)
    changed = closes[:41] + [c * 3.0 for c in closes[41:]]
    assert rv_known_at(stamps, changed, t) == before


def test_atm_iv_forward_interpolates_in_log_moneyness() -> None:
    F = 100.0
    exp = {"F": F, "strikes": {90.0: {"mark_iv": 0.60}, 110.0: {"mark_iv": 0.40}}}
    x0, x1 = math.log(90 / F), math.log(110 / F)
    w = (0.0 - x0) / (x1 - x0)
    assert atm_iv_forward(exp) == pytest.approx(0.60 + w * (0.40 - 0.60))
    assert atm_iv_forward({"F": F, "strikes": {100.0: {"mark_iv": 0.5}}}) == 0.5
    assert atm_iv_forward({"F": F, "strikes": {}}) is None


def test_ratio_and_condition() -> None:
    assert iv_rv_ratio(0.5, 0.4) == pytest.approx(1.25)
    assert iv_rv_ratio(None, 0.4) is None
    assert iv_rv_ratio(0.5, 0.0) is None
    assert condition_met(1.0) is True
    assert condition_met(0.999) is False
    assert condition_met(None) is None
    assert condition_met(1.1, thresh=1.2) is False


def test_dvol_known_at_uses_closed_candle_only() -> None:
    candles = [(D0, 0.50), (D0 + DAY_MS, 0.55), (D0 + 2 * DAY_MS, 0.90)]
    entry = D0 + 2 * DAY_MS + 9 * 3_600_000  # 09:00 of day 2: candle of day 1 is the last closed one
    assert dvol_known_at(candles, entry) == 0.55
    assert dvol_known_at(candles, D0) is None
    # no look-ahead: changing the unclosed candle does not change the value
    assert dvol_known_at([*candles[:2], (D0 + 2 * DAY_MS, 5.0)], entry) == 0.55
