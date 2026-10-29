"""Tests for analytics.weekend_reversal (pure functions, analytic expectations)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradedesk.analytics.funding_crowding import Bars
from tradedesk.analytics.weekend_reversal import (
    HOUR_MS,
    HourBar,
    HourBars,
    calendar_window,
    capped_weight,
    cross_ic,
    dominated,
    funding_sum,
    idio_return,
    maker_short_filled,
    quintile_size,
    span_idio,
    tercile_labels,
    top_k,
    turnover_ratio,
    weekday,
)

SAT = int(datetime(2026, 10, 24, tzinfo=UTC).timestamp()) // 86400  # a Saturday


def hour_of(y: int, m: int, d: int, h: int) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp()) // 3600


def flat(hours: range, price: float = 100.0) -> HourBars:
    return HourBars({h: HourBar(price, price, price, price, 1.0) for h in hours})


def test_weekday_index() -> None:
    assert weekday(SAT) == 5
    assert weekday(SAT + 2) == 0  # Monday
    assert weekday(0) == 3  # 1970-01-01 was a Thursday


def test_calendar_window_registered_hours() -> None:
    w = calendar_window(SAT)
    assert w.sig_start == hour_of(2026, 10, 24, 0)  # Friday close = Saturday 00:00
    assert w.sig_end == hour_of(2026, 10, 25, 22)  # Sunday 22:00
    assert w.tgt_start == hour_of(2026, 10, 26, 1)  # Monday 01:00
    assert w.tgt_end == hour_of(2026, 10, 27, 1)  # Tuesday 01:00
    assert w.tgt_start - w.sig_end == 3  # no shared print
    tue = calendar_window(SAT + 3)
    assert tue.tgt_start == hour_of(2026, 10, 29, 1)  # weekday placebo: Thursday 01:00
    with pytest.raises(ValueError):
        calendar_window(SAT, sig_end_hour=24)
    with pytest.raises(ValueError):
        calendar_window(SAT, hold_hours=0)


def test_idio_return_beta_neutral() -> None:
    assert idio_return(100, 110, 100, 105, 2.0) == pytest.approx(0.0)
    assert idio_return(100, 110, 100, 105, 0.0) == pytest.approx(0.10)
    assert idio_return(50, 45, 200, 220, 1.0) == pytest.approx(-0.2)


def test_hour_bars_from_rows_and_exact_price() -> None:
    t = hour_of(2026, 10, 24, 0) * HOUR_MS
    rows = [
        [t, 10, 11, 9, 10.5, 5, 50],
        [t + HOUR_MS, 10.5, 10.6, 10.4, 10.5, 0, 0],  # no trades: dropped
        [t + 2 * HOUR_MS, 10.5, 10.7, 10.1, 10.2, 3, 30],
    ]
    hb = HourBars.from_rows(rows)
    h0 = t // HOUR_MS
    assert hb.price_at(h0) == 10
    assert hb.price_at(h0 + 1) is None  # exact hour only, no fallback
    assert hb.price_at(h0 + 2) == 10.5
    assert hb.bar(h0 + 2) == HourBar(10.5, 10.7, 10.1, 10.2, 30)


def test_span_idio_missing_price_is_none() -> None:
    w = calendar_window(SAT)
    alt = flat(range(w.sig_start, w.sig_end))  # sig_end missing
    btc = flat(range(w.sig_start, w.tgt_end + 1))
    assert span_idio(alt, btc, w.sig_start, w.sig_end, 1.0) is None


def test_signal_does_not_see_data_after_its_end() -> None:
    w = calendar_window(SAT)
    hours = range(w.sig_start - 48, w.tgt_end + 48)
    alt = HourBars({h: HourBar(100 + 0.1 * (h - w.sig_start), 1e9, 1, 1, 1) for h in hours})
    btc = HourBars({h: HourBar(50 + 0.01 * (h - w.sig_start), 1e9, 1, 1, 1) for h in hours})
    w1 = span_idio(alt, btc, w.sig_start, w.sig_end, 1.3)
    t1 = span_idio(alt, btc, w.tgt_start, w.tgt_end, 1.3)
    # change everything strictly after the signal's last print (incl. the target window)
    alt2 = HourBars({h: (b if h <= w.sig_end else HourBar(b.open * 3, 1e9, 1, 1, 1)) for h, b in alt.bars.items()})
    btc2 = HourBars({h: (b if h <= w.sig_end else HourBar(b.open * 0.5, 1e9, 1, 1, 1)) for h, b in btc.bars.items()})
    assert span_idio(alt2, btc2, w.sig_start, w.sig_end, 1.3) == pytest.approx(w1)
    # the target is not affected by data before its first print
    alt3 = HourBars({h: (b if h >= w.tgt_start else HourBar(b.open * 7, 1e9, 1, 1, 1)) for h, b in alt.bars.items()})
    assert span_idio(alt3, btc, w.tgt_start, w.tgt_end, 1.3) == pytest.approx(t1)


def day_bars(qv: dict[int, float]) -> Bars:
    return Bars({d: 1.0 for d in qv}, {d: 1.0 for d in qv}, {d: 1.0 for d in qv}, {d: 1.0 for d in qv}, dict(qv))


def test_turnover_ratio_weekend_vs_weekday_median() -> None:
    qv = {}
    for d in range(SAT - 28, SAT + 7):
        qv[d] = 50.0 if weekday(d) >= 5 else 100.0
    qv[SAT - 3] = 1e6  # one weekday outlier: the median ignores it
    b = day_bars(qv)
    assert turnover_ratio(b, SAT) == pytest.approx(0.5)
    # days after the window do not matter
    qv2 = dict(qv)
    qv2[SAT + 3] = 1e9
    assert turnover_ratio(day_bars(qv2), SAT) == pytest.approx(0.5)
    del qv2[SAT + 1]
    assert turnover_ratio(day_bars(qv2), SAT) is None  # Sunday bar missing
    assert turnover_ratio(day_bars({SAT: 1.0, SAT + 1: 1.0}), SAT) is None  # no reference days


def test_funding_sum_bounds() -> None:
    rec = [[1000, 8, 0.001], [2000, 8, 0.002], [3000, 8, -0.0005]]
    assert funding_sum(rec, 1000, 3000) == pytest.approx(0.0015)  # (lo, hi]
    assert funding_sum(rec, 0, 2000) == pytest.approx(0.003)
    assert funding_sum([], 0, 10) == 0.0


def test_maker_short_fill_rule() -> None:
    assert maker_short_filled(HourBar(10, 10.01, 9, 9.5, 1))
    assert not maker_short_filled(HourBar(10, 10, 9, 9.5, 1))


def test_top_k_and_weights() -> None:
    v = {"A": 3.0, "B": 1.0, "C": 3.0, "D": -2.0}
    assert top_k(v, 2) == ["A", "C"]
    assert top_k(v, 1, largest=False) == ["D"]
    assert quintile_size(199) == 39
    assert quintile_size(3) == 1
    assert capped_weight(40) == pytest.approx(0.025)
    assert capped_weight(10) == pytest.approx(0.05)
    assert capped_weight(0) == 0.0


def test_tercile_labels() -> None:
    lab = tercile_labels([5, 1, 9, 3, 7, 2, 8, 4, 6])
    assert lab == [1, 0, 2, 0, 2, 0, 2, 1, 1]
    assert sorted(tercile_labels(list(range(10)))).count(2) == 3


def test_cross_ic_common_symbols() -> None:
    sig = {f"S{i}": float(i) for i in range(60)}
    tgt = {f"S{i}": -2.0 * i for i in range(5, 70)}
    assert cross_ic(sig, tgt) == pytest.approx(-1.0)
    assert cross_ic(sig, tgt, min_n=100) is None


def test_dominated() -> None:
    base = [0.01 * (i % 5 - 2) for i in range(50)]
    assert not dominated(base)
    assert dominated([*base, 5.0])
    assert not dominated([1.0, 1.0, 1.0, 1.0])  # MAD 0
