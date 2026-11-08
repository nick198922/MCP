"""Item 208 (standing improvements): the token-unlock and holders-revenue research scripts tag trades / events whose
funding has a hole inside the holding window (the paper bench rule), count them, and never fill the hole."""

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_holders_revenue as hr  # noqa: E402
import verify_token_unlocks as tu  # noqa: E402

DAY_S = 86_400
H8 = 8 * 3_600
RATE = 0.0001


def _bars(seed: int, last_day: int) -> list[list[float]]:
    rnd, px, out = random.Random(seed), 100.0, []
    for d in range(last_day + 1):
        px *= math.exp(rnd.gauss(0.0, 0.03))
        out.append([d, px, px * 1.02, px * 0.98, px, 1_000_000.0])
    return out


def _funding(last_s: int, *, hole: tuple[int, int] | None = None) -> list[tuple[int, float]]:
    """8 h payments (unix seconds) from day 0 to ``last_s``; ``hole`` = (from_s, to_s) drops the payments inside."""
    return [(t, RATE) for t in range(0, last_s + 1, H8) if hole is None or not hole[0] <= t < hole[1]]


# ----------------------------------------------------------------------------- token unlocks

T, ENTRY, EXIT = 200, 190, 202  # unlock day, entry close T-10, exit close T+2


def _tu_data(tok_funding: list[tuple[int, float]] | None, *, last_bar: int = 215) -> tu.Data:
    data = tu.Data.__new__(tu.Data)
    data.cache = Path("/nonexistent")
    data.btc = tu.Bars(_bars(1, 215))
    data.legs = {"tok": [tu.Leg(tu.BIN, "TOK", 1, tu.Bars(_bars(2, last_bar)))]}
    data.funding = {(tu.BIN, "BTC"): _funding(216 * DAY_S), (tu.BIN, "TOK"): tok_funding}
    data.funding_ms, data.gaps = {}, {}
    return data


def test_token_unlock_trade_is_tagged_on_a_hole_and_keeps_its_funding_sum():
    hole = ((195 * DAY_S), (197 * DAY_S))  # six payments missing mid-window
    tr = tu.trade(_tu_data(_funding(216 * DAY_S, hole=hole)), "tok", T, ENTRY, EXIT)
    assert tr is not None and tr.funding_gap is True and not tr.funding_missing
    # not filled: the sum covers only the payments that exist in ((entry+1) d, (exit+1) d]
    n_paid = sum(1 for t, _ in _funding(216 * DAY_S, hole=hole) if (ENTRY + 1) * DAY_S < t <= (EXIT + 1) * DAY_S)
    assert tr.f_tok == pytest.approx(n_paid * RATE)
    assert n_paid == 12 * 3 - 6


def test_token_unlock_trade_without_hole_and_missing_file_are_not_tagged():
    ok = tu.trade(_tu_data(_funding(216 * DAY_S)), "tok", T, ENTRY, EXIT)
    assert ok is not None and ok.funding_gap is False
    miss = tu.trade(_tu_data(None), "tok", T, ENTRY, EXIT)  # no file: counted as funding_missing, not as a gap
    assert miss is not None and miss.funding_missing is True and miss.funding_gap is False
    empty = tu.trade(_tu_data([]), "tok", T, ENTRY, EXIT)  # an empty file is a hole over the whole window
    assert empty is not None and empty.funding_gap is True


def test_token_unlock_perp_end_inside_the_window_is_not_a_hole():
    # the perp's bars stop on day 196 and its funding at 08:00 that day: a delisting, the exit is truncated
    data = _tu_data(_funding(196 * DAY_S + H8), last_bar=196)
    tr = tu.trade(data, "tok", T, ENTRY, EXIT)
    assert tr is not None and tr.truncated and tr.exit == 196 and tr.funding_gap is False
    # without stopping the window at the perp's end the 16 h tail would read as a hole
    assert data.funding_gap(tu.BIN, "TOK", (ENTRY + 1) * DAY_S, (196 + 1) * DAY_S) is True


def test_token_unlock_btc_leg_hole_tags_the_trade():
    data = _tu_data(_funding(216 * DAY_S))
    data.funding[(tu.BIN, "BTC")] = _funding(216 * DAY_S, hole=(193 * DAY_S, 195 * DAY_S))
    tr = tu.trade(data, "tok", T, ENTRY, EXIT)
    assert tr is not None and tr.funding_gap is True


# ----------------------------------------------------------------------------- holders revenue

ENTRY_HR = 150  # horizon 60 -> exit 210


def _hr_data(hole_in: str | None) -> hr.Data:
    data = hr.Data.__new__(hr.Data)
    data.cache = Path("/nonexistent")
    data.btc = hr.Bars(_bars(1, 220))
    keys = ["A", "B", "C", "D"]
    data.tab = {"tokens": {k: {"symbol": k} for k in keys}}
    data.legs = {k: [(hr.BIN, k, 1, hr.Bars(_bars(10 + i, 220)))] for i, k in enumerate(keys)}
    hole = (170 * DAY_S, 172 * DAY_S)
    data.funding = {(hr.BIN, "BTC"): _funding(221 * DAY_S)}
    for k in keys:
        data.funding[(hr.BIN, k)] = _funding(221 * DAY_S, hole=hole if k == hole_in else None)
    data.funding_ms, data.gaps = {}, {}
    return data


def test_holders_revenue_row_is_tagged_when_a_control_leg_has_a_hole():
    data = _hr_data("C")
    rows = hr.evaluate(data, {}, dates={"A": [ENTRY_HR]})
    assert len(rows) == 1 and sorted(rows[0].controls) == ["B", "C", "D"]
    r = rows[0]
    assert r.funding_gap is True and hr.leg_gap(data, r.obs) is False
    # the control's funding sum is what exists, not filled: 60 days x 3 minus the 6 missing payments
    oc = hr.observe(data, "C", ENTRY_HR, hr.HORIZON)
    assert oc is not None and hr.leg_gap(data, oc) is True
    assert oc.leg.funding == pytest.approx((60 * 3 - 6) * RATE)


def test_holders_revenue_rows_without_hole_or_with_event_leg_hole():
    assert hr.evaluate(_hr_data(None), {}, dates={"A": [ENTRY_HR]})[0].funding_gap is False
    data = _hr_data("A")
    r = hr.evaluate(data, {}, dates={"A": [ENTRY_HR]})[0]
    assert r.funding_gap is True and hr.leg_gap(data, r.obs) is True
