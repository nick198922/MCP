"""Item 383: carry B leg-price gap classification (analytics.carry_leg_gap)."""

from __future__ import annotations

import pytest

from tradedesk.analytics import carry_leg_gap as g

H = 3_600_000
C0 = 16 * H  # a cycle at 16:00 UTC (day 0); the engine decides at 16:05 and prices off the bar 12:00-16:00


def _bars(closes: dict[int, float], vol: float = 10.0) -> dict[int, list[float]]:
    return {k: [c, c, c, c, vol] for k, c in closes.items()}


def _rows(pairs: list[tuple[float, float]]) -> list[dict[str, float | int]]:
    return [{"cycle_ts": C0 + i * 8 * H, "price_a": a, "price_b": b} for i, (a, b) in enumerate(pairs)]


def _indep(pairs: list[tuple[float, float]], vol: float = 10.0) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
    ka = {g.cycle_bar_open(C0 + i * 8 * H): a for i, (a, _) in enumerate(pairs)}
    kb = {g.cycle_bar_open(C0 + i * 8 * H): b for i, (_, b) in enumerate(pairs)}
    return _bars(ka, vol), _bars(kb, vol)


def test_cycle_bar_open_is_the_bar_closing_at_the_cycle() -> None:
    assert g.cycle_bar_open(C0) == 12 * H
    assert g.cycle_bar_open(0) == -4 * H


def test_longest_equal_run() -> None:
    assert g.longest_equal_run([1.0, 1.0, 2.0, 2.0, 2.0, None, 2.0]) == 3
    assert g.longest_equal_run([None, None]) == 0
    assert g.longest_equal_run([5.0]) == 1


def test_market_persistent_gap() -> None:
    pairs = [(0.99, 1.0), (1.98, 2.0), (2.97, 3.0)]  # a 1 % below b in every row, bars agree
    checks = g.row_checks(_rows(pairs), *_indep(pairs))
    assert checks[0]["gap"] == pytest.approx(-0.01)
    assert checks[0]["dev_a"] == pytest.approx(0.0)
    m = g.minute_gap({0: 0.99, 60_000: 1.98}, {0: 1.0, 60_000: 2.0}, 0, 120_000)
    assert m["n"] == 2 and m["median"] == pytest.approx(-0.01) and m["share_over"] == 1.0
    v = g.classify(checks, m)
    assert (v["verdict"], v["cls"], v["flags"]) == ("✗", "market, persistent", [])
    assert v["median_indep"] == pytest.approx(-0.01)


def test_market_close_noise() -> None:
    pairs = [(0.99, 1.0), (1.98, 2.0), (2.97, 3.0)]
    v = g.classify(g.row_checks(_rows(pairs), *_indep(pairs)), {"median": 0.001})
    assert (v["verdict"], v["cls"]) == ("✗", "market, close noise")


def test_frozen_leg_explains_failure() -> None:
    engine = [(1.0, 1.0), (1.0, 1.1), (1.0, 1.2)]  # leg a stuck at 1.0, the venue's bars move with b
    indep = [(1.0, 1.0), (1.1, 1.1), (1.2, 1.2)]
    checks = g.row_checks(_rows(engine), *_indep(indep))
    v = g.classify(checks, {"median": 0.0})
    assert v["verdict"] == "✓" and "R1 frozen a" in v["flags"] and "R2 foreign bar a" in v["flags"]
    assert v["median_indep"] == pytest.approx(0.0)


def test_defect_that_does_not_explain_the_gap() -> None:
    pairs = [(0.99, 1.0), (0.99, 1.0), (0.99, 1.0)]  # both stuck in the engine, but the bars show the same 1 % gap
    v = g.classify(g.row_checks(_rows(pairs), *_indep(pairs, vol=0.0)), {"median": -0.01})
    assert v["verdict"] == "≈" and "R1 zero volume a" in v["flags"]


def test_units_and_no_data() -> None:
    pairs = [(1000.0, 1.0), (1000.0, 1.01), (1001.0, 1.0)]
    assert "R4 units" in g.classify(g.row_checks(_rows(pairs), *_indep(pairs)), {"median": None})["flags"]
    assert g.classify(g.row_checks(_rows(pairs), {}, {}), {})["verdict"] == "◯"
    assert g.classify([], {})["verdict"] == "◯"


def test_missing_bar_makes_r2_unknown() -> None:
    pairs = [(0.99, 1.0), (1.98, 2.0), (2.97, 3.0)]
    ba, bb = _indep(pairs)
    full = g.classify(g.row_checks(_rows(pairs), ba, bb), {"median": -0.01})
    assert (full["verdict"], full["no_bar"], full["r2_unknown"]) == ("✗", {"a": 0, "b": 0}, False)
    # the bar behind the third row is missing on leg a: a foreign bar could hide there, so no market verdict
    del ba[g.cycle_bar_open(C0 + 16 * H)]
    v = g.classify(g.row_checks(_rows(pairs), ba, bb), {"median": -0.01})
    assert (v["verdict"], v["no_bar"], v["r2_unknown"], v["flags"]) == ("◯", {"a": 1, "b": 0}, True, [])
    assert v["cls"].startswith("R2 unknown")
    # a zero close is no usable bar either
    bb[g.cycle_bar_open(C0)] = [1.0, 1.0, 1.0, 0.0, 10.0]
    assert g.classify(g.row_checks(_rows(pairs), ba, bb), {})["no_bar"] == {"a": 1, "b": 1}


def test_missing_bar_marks_a_defect_verdict() -> None:
    engine = [(1.0, 1.0), (1.0, 1.1), (1.0, 1.2)]
    ba, bb = _indep([(1.0, 1.0), (1.1, 1.1), (1.2, 1.2)])
    del bb[g.cycle_bar_open(C0 + 16 * H)]
    v = g.classify(g.row_checks(_rows(engine), ba, bb), {"median": 0.0})
    assert v["verdict"] == "✓" and "R1 frozen a" in v["flags"] and v["r2_unknown"]
    assert v["cls"].startswith("engine; R2 unknown on 0/1 rows")


def test_minute_gap_closing_minutes_and_window() -> None:
    a = {k * 60_000: 1.0 for k in range(240)} | {4 * H - 60_000: 1.02}
    b = {k * 60_000: 1.0 for k in range(240)}
    m = g.minute_gap(a, b, 0, 4 * H)
    assert m["n"] == 240 and m["median"] == pytest.approx(0.0)
    assert m["n_closing"] == 1 and m["median_closing"] == pytest.approx(0.02)


def test_earlier_results_do_not_depend_on_later_data() -> None:
    pairs = [(0.99, 1.0), (1.98, 2.0), (2.97, 3.0)]
    ba, bb = _indep(pairs)
    first = g.row_checks(_rows(pairs)[:2], ba, bb)
    ba2 = dict(ba)
    ba2[g.cycle_bar_open(C0 + 16 * H)] = [9.0, 9.0, 9.0, 9.0, 0.0]  # change only the bar behind the third row
    assert g.row_checks(_rows(pairs)[:2], ba2, bb) == first
    a1 = {0: 1.0, 60_000: 1.0}
    b1 = {0: 1.0, 60_000: 1.0}
    m = g.minute_gap(a1, b1, 0, 120_000)
    assert g.minute_gap(a1 | {120_000: 5.0}, b1 | {120_000: 1.0}, 0, 120_000) == m
