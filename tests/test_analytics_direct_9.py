"""Direct tests for ten analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 196).

They feed the trade numbers of research reports and forwards: the stop and target of the S14 course setup
(`course_s14_ema.stop_target`), the sieve's trade at fixed stop / target fractions (`strategy_sieve.entry_outcome`),
the session stop-hunt targets (`session_stop_hunt.target_for`), the next-expiry ATM mark IV of the event straddle
quote (`event_straddle.next_expiry_atm_iv`), the H10 runner's break-even rule (`s3_modules.runner_rule`), the exit
close of the pre-market fade (`premarket_fade.close_at`) and the levels / indicators of four sieve specs
(`williams_vol_breakout.day_levels`, `mayer_multiple_fade.mayer`, `pretty_good_oscillator.pgo`,
`kst_signal_cross.roc`). References are hand-computed in the comments (the IV one inverts a Black-76 price written
out here); no output of the code under test is reused as an expected value.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import Bars, Ind, Params, stop_target
from tradedesk.analytics.event_straddle import next_expiry_atm_iv
from tradedesk.analytics.premarket_fade import close_at
from tradedesk.analytics.s3_modules import runner_rule
from tradedesk.analytics.session_stop_hunt import Setup, target_for
from tradedesk.analytics.sieve_specs.kst_signal_cross import roc
from tradedesk.analytics.sieve_specs.mayer_multiple_fade import mayer
from tradedesk.analytics.sieve_specs.pretty_good_oscillator import pgo
from tradedesk.analytics.sieve_specs.williams_vol_breakout import day_levels
from tradedesk.analytics.stop_management import Fees, StopRule
from tradedesk.analytics.strategy_sieve import entry_outcome

HOUR = 3_600_000
DAY = 24 * HOUR
NO_FEES = Fees(0.0, 0.0, 0.0, 0.0)


def _bars(o, h, low, c, *, bar_ms: int = HOUR, t=None) -> Bars:
    n = len(c)
    t = np.arange(n, dtype=np.int64) * bar_ms if t is None else np.asarray(t, dtype=np.int64)
    return Bars(t, np.asarray(o, float), np.asarray(h, float), np.asarray(low, float), np.asarray(c, float), bar_ms)


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


# ------------------------------------------------------------------ course_s14_ema.stop_target


def _ind(atr: list[float]) -> Ind:
    n = len(atr)
    z = np.zeros(n)
    zi = np.zeros(n, dtype=np.int64)
    return Ind(zi, z, z, z, np.asarray(atr, float), zi, zi, np.zeros(n, dtype=bool))


def test_stop_target_half_atr_under_the_low_and_impulse_high_or_2r() -> None:
    # close 100, low 98, ATR 4: stop = 98 - 0.5 * 4 = 96, risk 4; the impulse high counts when >= 1.5R = 6 away
    b = _bars([99.0], [101.0], [98.0], [100.0])
    ind, p = _ind([4.0]), Params()
    assert stop_target(b, ind, p, 0, 106.0) == (96.0, 106.0, "high")  # exactly 1.5R: the high
    stop, tgt, kind = stop_target(b, ind, p, 0, 105.9)  # 5.9 < 6: fixed 2R = 100 + 8
    assert (stop, kind) == (96.0, "fixed") and tgt == pytest.approx(108.0, abs=1e-12)
    custom = Params(stop_atr=1.0, min_target_r=1.0, fallback_r=3.0)  # stop 94, risk 6: 104 < 1R -> 100 + 18
    assert stop_target(b, ind, custom, 0, 104.0) == (94.0, 118.0, "fixed")


def test_stop_target_none_without_atr_or_risk() -> None:
    b = _bars([100.0, 100.0], [101.0, 101.0], [100.0, 100.0], [100.0, 100.0])
    assert stop_target(b, _ind([math.nan, 4.0]), Params(), 0, 110.0) is None
    assert stop_target(b, _ind([0.0, 4.0]), Params(), 0, 110.0) is None
    assert stop_target(b, _ind([4.0, 4.0]), Params(stop_atr=0.0), 1, 110.0) is None  # low == close: zero risk


# ------------------------------------------------------------------ strategy_sieve.entry_outcome


def _path() -> Bars:
    # 1h bars from t = 0; the entry is the close of bar 0 (100), held from t = 1h
    return _bars(
        o=[99.0, 100.0, 100.5, 104.0],
        h=[100.5, 101.0, 104.5, 105.0],
        low=[98.5, 99.5, 100.0, 103.0],
        c=[100.0, 100.5, 104.0, 104.5],
    )


def test_entry_outcome_long_target_without_and_with_fees() -> None:
    # stop 2 % under 100 = 98, target 4 % over = 104, touched by bar 2 (high 104.5): +4 / risk 2 = +2R
    o, e, st = entry_outcome(_path(), 0, 0.02, 0.04, 3 * HOUR, StopRule("none"), NO_FEES)
    assert (e, st) == (100.0, 98.0)
    assert (o.exit_kind, o.exit) == ("target", 104.0) and o.exit_ms == 3 * HOUR
    assert o.r_gross == pytest.approx(2.0, abs=1e-12) and o.cost_r == 0.0
    # default fees: risk unit 2 + 0.00055 * (100 + 98) = 2.1089; maker in and out: 0.0002 * (100 + 104)
    o, _, _ = entry_outcome(_path(), 0, 0.02, 0.04, 3 * HOUR, StopRule("none"))
    assert o.r_gross == pytest.approx(4 / 2.1089, rel=1e-12)
    assert o.cost_r == pytest.approx(0.0408 / 2.1089, rel=1e-12)


def test_entry_outcome_infinite_target_exits_by_time_and_short_on_mirrored_path() -> None:
    # no target, 2h hold: bars 1 and 2 are held, the close of bar 2 (104) exits by time: +4 / 2 = +2R
    o, _, _ = entry_outcome(_path(), 0, 0.02, math.inf, 2 * HOUR, StopRule("none"), NO_FEES)
    assert (o.exit_kind, o.exit) == ("time", 104.0) and o.r_gross == pytest.approx(2.0, abs=1e-12)
    # a short on a falling path: entry 100, stop 102, target 96 hit by bar 2 (low 95.5); mirrored prices are negated
    down = _bars(o=[101.0, 100.0, 99.5], h=[101.5, 100.5, 100.0], low=[99.5, 99.0, 95.5], c=[100.0, 99.5, 96.0])
    o, e, st = entry_outcome(down.mirrored(), 0, 0.02, 0.04, 3 * HOUR, StopRule("none"), NO_FEES)
    assert (e, st) == (-100.0, -102.0)
    assert (o.exit_kind, o.exit) == ("target", -96.0) and o.r_gross == pytest.approx(2.0, abs=1e-12)
    # the last bar has no path after it
    assert entry_outcome(_path(), 3, 0.02, 0.04, 3 * HOUR, StopRule("none"), NO_FEES) is None


# ------------------------------------------------------------------ session_stop_hunt.target_for


def test_target_for_t1_opposite_edge_from_1r_and_t2_fixed_2r() -> None:
    # short space: entry 100, stop 102 (risk 2); T1 = range low only if it is >= 1R below the entry
    st = Setup(-1, 103.0, 98.0, 0, 0, 103.0, 0, 101.0, 1, 100.0, 102.0, 5)
    assert target_for("T1", st, 100.0) == 98.0  # exactly 1R
    assert target_for("T1", st, 99.5) is None  # risk 2.5, edge 1.5 away
    assert target_for("T2", st, 100.0) == 96.0  # 100 - 2 * 2
    assert target_for("T2", st, 101.0) == 99.0  # E2 level 101: risk 1, 101 - 2


# ------------------------------------------------------------------ event_straddle.next_expiry_atm_iv


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _b76_inverse(F: float, K: float, T: float, s: float, call: bool) -> float:
    """Black-76 with r = 0, divided by F (Deribit inverse price in BTC)."""
    n = _ncdf
    d1 = (math.log(F / K) + 0.5 * s * s * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    usd = F * n(d1) - K * n(d2) if call else K * n(-d2) - F * n(-d1)
    return usd / F


EXP = _ms(2025, 1, 15, 8)  # Deribit expiry 08:00 UTC
T0 = _ms(2025, 1, 14, 9)
LO = T0 - 2 * HOUR
IDX, RATIO = 100_000.0, 1.001  # forward = 1.001 * index = 100 100


def _print(name: str, ts: int, sigma: float | None) -> dict:
    exp = EXP if "15JAN25" in name else EXP + DAY
    K, call = float(name.split("-")[2]), name.endswith("-C")
    mark = None if sigma is None else _b76_inverse(RATIO * IDX, K, (exp - ts) / (365 * DAY), sigma, call)
    return {"instrument_name": name, "timestamp": ts, "index_price": IDX, "mark_price": mark}


def test_next_expiry_atm_iv_is_the_median_mark_iv_at_the_strike_nearest_the_forward() -> None:
    trades = [
        _print("BTC-15JAN25-100000-C", T0 - 90 * 60_000, 0.50),
        _print("BTC-15JAN25-100000-P", T0 - 60 * 60_000, 0.52),
        _print("BTC-15JAN25-100000-C", T0 - 30 * 60_000, 0.54),
        _print("BTC-15JAN25-101000-C", T0 - 20 * 60_000, 0.70),  # 900 from F0, 100 000 is 100 away
        _print("BTC-15JAN25-100000-P", T0 + 60_000, 0.90),  # after t0
        _print("BTC-15JAN25-100000-C", LO - 1, 0.90),  # before the window
        _print("BTC-16JAN25-100000-C", T0 - 10 * 60_000, 0.90),  # another expiry
    ]
    F0 = RATIO * IDX
    # both legs pooled: median of 0.50, 0.52, 0.54 (bisection to 1e-7)
    assert next_expiry_atm_iv(trades, EXP, LO, T0, F0, RATIO) == pytest.approx(0.52, abs=1e-6)
    # only the two freshest prints: median of 0.52 and 0.54
    assert next_expiry_atm_iv(trades, EXP, LO, T0, F0, RATIO, max_prints=2) == pytest.approx(0.53, abs=1e-6)


def test_next_expiry_atm_iv_none_when_far_strike_no_mark_or_no_prints() -> None:
    F0 = RATIO * IDX
    far = [_print("BTC-15JAN25-102000-C", T0 - HOUR, 0.5)]  # 102 000 / 100 100 - 1 = 1.9 % > 1.5 %
    assert next_expiry_atm_iv(far, EXP, LO, T0, F0, RATIO) is None
    no_mark = [_print("BTC-15JAN25-100000-C", T0 - HOUR, None)]
    assert next_expiry_atm_iv(no_mark, EXP, LO, T0, F0, RATIO) is None
    assert next_expiry_atm_iv([], EXP, LO, T0, F0, RATIO) is None


# ------------------------------------------------------------------ s3_modules.runner_rule


def test_runner_rule_moves_to_break_even_halfway_to_mid() -> None:
    # entry 100, mid 104, far 110: be_frac = 0.5 * 4 / 10 = 0.2, trigger = 100 + 0.2 * 10 = 102 (half the way to mid)
    r = runner_rule(100.0, 104.0, 110.0)
    assert r.name == "H10" and r.be_frac == pytest.approx(0.2, abs=1e-15)
    assert r.trigger(100.0, 98.0, 110.0) == pytest.approx(102.0, abs=1e-12)
    # mirrored short space: entry -100, mid -97, far -91 -> trigger -98.5
    r = runner_rule(-100.0, -97.0, -91.0, name="x")
    assert r.name == "x" and r.trigger(-100.0, -102.0, -91.0) == pytest.approx(-98.5, abs=1e-12)


# ------------------------------------------------------------------ premarket_fade.close_at


def test_close_at_reads_the_bar_ending_exactly_at_t_end() -> None:
    bars = {0: (9.0, 10.5, 8.5, 10.0), HOUR: (10.0, 11.5, 9.5, 11.0)}
    assert close_at(bars, HOUR) == 10.0
    assert close_at(bars, 2 * HOUR) == 11.0
    assert close_at(bars, HOUR + 1) is None  # no bar ends there
    assert close_at(bars, 3 * HOUR) is None


# ------------------------------------------------------------------ williams_vol_breakout.day_levels


def _days(spec: list[tuple[int, list[int]]], tweak: dict[tuple[int, int], tuple[float, float, float, float]]) -> Bars:
    """1h bars of UTC days (day number, hours present), flat 100 / 101 / 99 / 100 unless ``tweak`` sets o, h, l, c."""
    base = 20_000
    rows = []
    for d, hours in spec:
        for j in hours:
            o, h, lo, c = tweak.get((d, j), (100.0, 101.0, 99.0, 100.0))
            rows.append(((base + d) * DAY + j * HOUR, o, h, lo, c))
    return Bars.from_rows(rows, HOUR)


def test_day_levels_open_plus_minus_k_times_yesterdays_range() -> None:
    full = list(range(24))
    tw = {
        (0, 5): (100.0, 110.0, 99.0, 100.0),  # day 0: high 110, low 90 -> range 20
        (0, 7): (100.0, 101.0, 90.0, 100.0),
        (1, 0): (102.0, 103.0, 99.0, 100.0),  # day 1 opens 102; high 106, low 94 -> range 12
        (1, 3): (100.0, 106.0, 99.0, 100.0),
        (1, 9): (100.0, 101.0, 94.0, 100.0),
        (2, 0): (103.0, 103.5, 99.0, 100.0),  # day 2 opens 103
    }
    b = _days([(0, full), (1, full), (2, [0, 1, 2]), (4, [0, 1])], tw)  # day 3 missing
    starts, ends, lower, upper = day_levels(b, 0.5)
    assert starts.tolist() == [0, 24, 48, 51] and ends.tolist() == [24, 48, 51, 53]
    # day 0: no previous day; day 1: 102 -+ 10; day 2: 103 -+ 6; day 4: previous day missing
    np.testing.assert_allclose(lower, [np.nan, 92.0, 97.0, np.nan], atol=1e-12)
    np.testing.assert_allclose(upper, [np.nan, 112.0, 109.0, np.nan], atol=1e-12)
    _, _, lower, upper = day_levels(b, 0.25)  # day 1: 102 -+ 5
    assert (lower[1], upper[1]) == (97.0, 107.0)


def test_day_levels_nan_for_incomplete_previous_day_late_start_or_flat_day() -> None:
    no5 = [j for j in range(24) if j != 5]
    b = _days([(0, no5), (1, list(range(24))), (2, [1, 2, 3])], {})
    _, _, lower, upper = day_levels(b, 0.5)
    assert np.isnan(lower).all() and np.isnan(upper).all()  # day 1: 23 bars before it; day 2: starts at 01:00
    flat = {(0, j): (100.0, 100.0, 100.0, 100.0) for j in range(24)}
    _, _, lower, _ = day_levels(_days([(0, list(range(24))), (1, [0])], flat), 0.5)
    assert np.isnan(lower).all()  # yesterday's range 0
    starts, ends, lower, upper = day_levels(Bars.from_rows([], HOUR), 0.5)
    assert starts.size == ends.size == lower.size == upper.size == 0


# ------------------------------------------------------------------ mayer_multiple_fade.mayer


def test_mayer_is_close_over_sma_including_the_bar_and_restarts_per_segment() -> None:
    c = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    b = _bars(c, c, c, c, bar_ms=DAY)
    # one segment, n = 3: 3 / 2, 4 / 3, 5 / 4, 6 / 5
    m = mayer(b, np.arange(6), 3)
    np.testing.assert_allclose(m, [np.nan, np.nan, 1.5, 4 / 3, 1.25, 1.2], rtol=1e-12)
    # a new segment at bar 3 needs three bars of its own: only bar 5 = 6 / mean(4, 5, 6)
    m = mayer(b, np.array([0, 1, 2, 0, 1, 2]), 3)
    np.testing.assert_allclose(m, [np.nan, np.nan, 1.5, np.nan, np.nan, 1.2], rtol=1e-12)


# ------------------------------------------------------------------ pretty_good_oscillator.pgo


def test_pgo_distance_from_sma_in_ema_true_range_units() -> None:
    # h = c + 1, l = c - 1. TR: 2, 2, 2, then max(2, |15 - 12|, |13 - 12|) = 3. EMA3 seeded with mean 2, k = 0.5 -> 2.5
    c = np.array([10.0, 11.0, 12.0, 14.0])
    b = _bars(c, c + 1, c - 1, c, bar_ms=DAY)
    p = pgo(b, 3, np.arange(4))
    # bar 2: (12 - 11) / 2; bar 3: (14 - 37/3) / 2.5 = 2/3
    np.testing.assert_allclose(p, [np.nan, np.nan, 0.5, 2 / 3], rtol=1e-12)


def test_pgo_segment_start_ignores_previous_close_and_zero_range_gives_nan() -> None:
    c = np.array([10.0, 11.0, 12.0, 50.0, 51.0, 53.0])
    b = _bars(c, c + 1, c - 1, c, bar_ms=DAY)
    # segment 2 TR: 2 (no gap 38 from 12), 2, max(2, 3, 1) = 3 -> seed 7/3; bar 5: (53 - 154/3) / (7/3) = 5/7
    p = pgo(b, 3, np.array([0, 1, 2, 0, 1, 2]))
    np.testing.assert_allclose(p, [np.nan, np.nan, 0.5, np.nan, np.nan, 5 / 7], rtol=1e-12)
    flat = np.full(4, 100.0)
    assert np.isnan(pgo(_bars(flat, flat, flat, flat, bar_ms=DAY), 3, np.arange(4))).all()


# ------------------------------------------------------------------ kst_signal_cross.roc


def test_roc_is_percent_change_over_n_bars_within_a_segment() -> None:
    # percent, not a decimal: 110 / 100 -> 10, 99 / 110 -> -10, 0 / 99 -> -100, then a zero base -> NaN
    r = roc(np.array([100.0, 110.0, 99.0, 0.0, 5.0]), 1, np.arange(5))
    np.testing.assert_allclose(r, [np.nan, 10.0, -10.0, -100.0, np.nan], atol=1e-12)
    # n = 2 after a segment start at bar 2: only bar 4 = 100 * (60 / 50 - 1)
    r = roc(np.array([100.0, 110.0, 50.0, 55.0, 60.0]), 2, np.array([0, 1, 0, 1, 2]))
    np.testing.assert_allclose(r, [np.nan, np.nan, np.nan, np.nan, 20.0], atol=1e-12)
