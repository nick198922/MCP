import math
import statistics

import numpy as np
import pytest

from tradedesk.analytics.funding_crowding import Bars
from tradedesk.analytics.oi_flush import SEMANTICS_SWITCH_MS
from tradedesk.analytics.oi_turnover import (
    DAY_MS,
    MIN_MS,
    beta_residual,
    leverage_ratio,
    median_qv,
    neutralized_signal,
    oi_value_snapshot,
    quintiles,
    realized_vol,
    split_diff,
    turnover,
    weekly_ics,
)


def bars_from(closes: dict[int, float], qv: dict[int, float] | None = None) -> Bars:
    q = qv or {d: 1e6 for d in closes}
    return Bars(dict(closes), dict(closes), dict(closes), dict(closes), q)


def row(ms: int, oi: float, val: float) -> list[float]:
    return [ms, oi, val, 1.0, 1.0, 1.0, 1.0]


# ---------------------------------------------------------------- OI snapshot


def test_snapshot_takes_last_row_before_margin_old_semantics():
    day = 19_000  # 2022-01-08, before the switch: observation = create_time
    t0 = day * DAY_MS
    rows = [row(t0 + k * 5 * MIN_MS, 10.0 + k, 100.0 + k) for k in range(288)]  # 00:00 .. 23:55
    s = oi_value_snapshot(rows, day)
    assert s is not None
    assert s["obs_ms"] == t0 + 287 * 5 * MIN_MS  # 23:55 is allowed (cutoff = 23:55)
    assert s["oi_value"] == pytest.approx(100.0 + 287)


def test_snapshot_new_semantics_shifts_by_five_minutes():
    day = SEMANTICS_SWITCH_MS // DAY_MS + 10
    t0 = day * DAY_MS
    rows = [row(t0 + k * 5 * MIN_MS, 1.0, 100.0 + k) for k in range(288)]
    s = oi_value_snapshot(rows, day)
    # create 23:55 -> observed 00:00 next day (excluded); create 23:50 -> observed 23:55 (kept)
    assert s is not None and s["oi_value"] == pytest.approx(100.0 + 286)


def test_snapshot_skips_bad_values_and_empty():
    day = 19_000
    t0 = day * DAY_MS
    rows = [row(t0, 1.0, 50.0), row(t0 + 5 * MIN_MS, 1.0, float("nan")), row(t0 + 10 * MIN_MS, 1.0, 0.0)]
    assert oi_value_snapshot(rows, day)["oi_value"] == 50.0
    assert oi_value_snapshot([], day) is None
    assert oi_value_snapshot([row(t0 - 5 * MIN_MS, 1.0, 9.0)], day) is None  # previous day only


def test_snapshot_ignores_rows_after_cutoff():
    """Causality: changing rows observed after the decision time does not change the snapshot."""
    day = 19_000
    t0 = day * DAY_MS
    base = [row(t0 + k * 5 * MIN_MS, 1.0, 100.0 + k) for k in range(288)]
    later = base + [row(t0 + DAY_MS + k * 5 * MIN_MS, 1.0, 1e9) for k in range(10)]
    assert oi_value_snapshot(base, day) == oi_value_snapshot(later, day)


# ---------------------------------------------------------------- turnover, vol, L


def test_median_qv_window_and_min_days():
    qv = {d: float(d) for d in range(100, 120)}
    b = bars_from({d: 1.0 for d in qv}, qv)
    assert median_qv(b, 110) == statistics.median([103, 104, 105, 106, 107, 108, 109])
    assert median_qv(b, 102) is None  # only days 100, 101 before t
    assert median_qv(b, 102, min_days=2) == 100.5


def test_realized_vol_analytic():
    # alternating +r / -r log returns -> SD known exactly
    r = 0.02
    closes = {}
    lp = 0.0
    for i, d in enumerate(range(200, 241)):
        closes[d] = math.exp(lp)
        lp += r if i % 2 == 0 else -r
    b = bars_from(closes)
    t = 240
    rets = [math.log(closes[d] / closes[d - 1]) for d in range(t - 30, t)]
    assert realized_vol(b, t) == pytest.approx(statistics.stdev(rets))
    assert realized_vol(b, 215) is None  # fewer than 20 returns


def test_inputs_before_t_do_not_change_when_later_data_changes():
    closes = {d: 100.0 + d for d in range(0, 60)}
    qv = {d: 1e6 * (1 + d % 5) for d in range(0, 60)}
    b1 = bars_from(closes, qv)
    c2, q2 = dict(closes), dict(qv)
    for d in range(40, 60):
        c2[d] *= 3.0
        q2[d] *= 7.0
    b2 = bars_from(c2, q2)
    assert median_qv(b1, 40) == median_qv(b2, 40)
    assert realized_vol(b1, 40) == realized_vol(b2, 40)


def test_leverage_ratio():
    assert leverage_ratio(5e8, 1e9) == 0.5
    assert leverage_ratio(None, 1.0) is None
    assert leverage_ratio(1.0, 0.0) is None


def test_neutralized_signal_removes_linear_controls():
    rng = np.random.default_rng(1)
    x1, x2, e = rng.normal(size=200), rng.normal(size=200), rng.normal(size=200)
    y = 1.0 + 2.0 * x1 - 0.5 * x2 + e
    res = neutralized_signal(y, [x1, x2])
    assert abs(np.corrcoef(res, x1)[0, 1]) < 1e-9
    assert abs(np.corrcoef(res, x2)[0, 1]) < 1e-9
    assert abs(res.mean()) < 1e-9
    # exact linear signal -> zero residual
    assert np.allclose(neutralized_signal(3.0 * x1 + 1.0, [x1]), 0.0)


def test_beta_residual():
    assert beta_residual(0.10, 1.5, 0.04) == pytest.approx(0.04)


def test_quintiles_and_ics():
    sig = {f"S{i:02d}": float(i) for i in range(12)}
    bot, top = quintiles(sig)
    assert bot == ["S00", "S01"] and top == ["S10", "S11"]
    assert quintiles({"A": 1.0, "B": 2.0}) == ([], [])
    ics = weekly_ics([([1, 2, 3, 4], [4, 3, 2, 1]), ([1, 2, 3, 4], [1, 2, 3, 4])])
    assert ics[0] == pytest.approx(-1.0) and ics[1] == pytest.approx(1.0)


def test_turnover():
    assert turnover([], ["A", "B"]) == 1.0
    assert turnover(["A", "B"], ["A", "B"]) == 0.0
    assert turnover(["A", "B"], ["A", "C"]) == pytest.approx(1.0)  # half replaced: 0.5 out + 0.5 in
    assert turnover(["A", "B"], ["C", "D"]) == pytest.approx(2.0)
    assert turnover(["A"], []) == 1.0


def test_split_diff():
    d, se, na, nb = split_diff([1.0, 3.0, 0.0, 2.0], [True, True, False, False])
    assert d == pytest.approx(1.0)
    assert se == pytest.approx(math.sqrt(2.0 / 2 + 2.0 / 2))
    assert (na, nb) == (2, 2)
    assert math.isnan(split_diff([1.0, 2.0], [True, False])[0])
