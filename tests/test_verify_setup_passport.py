"""Wiring of S17 / S23 into ``scripts/verify_setup_passport.py`` (item 11 of IDEA-20261030-demo-setups-depth): limit
setups with each signal's own wait (``rest_until_ms``) and hold (S23 ``max_hold_ms``, S17 20 bars), S23's limit
cancelled by its target before the fill, the placebo target carried in percent for S23."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, resample  # noqa: E402

T_START = vsp.s1.ms("2021-01-04T00:00")
N_H = 1600
K4 = 350  # the signal's 4h bar
H_FILL = K4 * 4 + 4  # first 1h bar after the signal bar's close
LEVEL, STOP, TARGET = 99.0, 97.0, 102.0  # target not at 2R (S23 aims at the range middle)


def _h1(edits: dict[int, tuple[float, float]]) -> Bars:
    t = T_START + HOUR_MS * np.arange(N_H, dtype=np.int64)
    c = np.full(N_H, 100.0)
    lo, hi = np.full(N_H, 99.8), np.full(N_H, 100.2)
    for k, (a, b) in edits.items():
        lo[k], hi[k] = a, b
    return Bars(t, c.copy(), hi, lo, c, HOUR_MS)


def _sig(setup: str, tb: Bars, hold_ms: float | None) -> ss.LiveSignal:
    ts = int(tb.t[K4])
    ind: dict[str, Any] = {"rest_until_ms": float(ts + tb.bar_ms + 4 * HOUR_MS)}
    if hold_ms is not None:
        ind["max_hold_ms"] = hold_ms
    return ss.LiveSignal(setup, "long", "4h", ts, LEVEL, STOP, TARGET, "", ind)


def _run(setup: str, h1: Bars, hold_ms: float | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[float]]:
    tb = resample(h1, 4)
    seen_tg: list[float] = []

    def ctx(t_in: int, side: str, e: float, st: float, tg: float) -> dict[str, Any]:
        seen_tg.append(tg)
        return {
            "sym": "AAAUSDT",
            "side": side,
            "t_in": t_in,
            "top30": False,
            "turn": 1e7,
            "grp": "ge5m",
            "rr": 2.0,
            "stop_pct": 0.02,
            "score": 3,
            "regime": None,
        }

    rows, pl = vsp.limit_rows(setup, "AAAUSDT", "4h", tb, h1, [_sig(setup, tb, hold_ms)], ctx, {}, Counter())
    return rows, pl, seen_tg


def test_s17_s23_are_limit_setups_on_the_default_demo_list():
    assert {"S17", "S23"} <= set(vsp.LIMIT) and vsp.CANCEL_AT_TARGET == ("S23",)
    assert vsp.SETUPS["S17"][1] == ("1d", "4h") and vsp.SETUPS["S23"][1] == ("4h",)
    assert vsp.SETUPS["S17"][2] == vsp.SETUPS["S23"][2] == 5e6 and "S17" not in vsp.WIDE and "S23" not in vsp.WIDE
    assert vsp.LIMIT_WARMUP["S23"] == ss.S23_WINDOW - 1 and vsp.LIMIT_WARMUP["S17"] == ss.S17_WARMUP


def test_own_hold_from_the_signal_fields():
    tb = resample(_h1({}), 4)
    assert vsp.own_hold("S23", _sig("S23", tb, float(ss.S23_HOLD_MS)), "4h", tb.bar_ms) == 10 * 24 * HOUR_MS
    assert vsp.own_hold("S17", _sig("S17", tb, None), "4h", tb.bar_ms) == ss.S17_HOLD_BARS * 4 * HOUR_MS
    assert vsp.own_hold("S17", _sig("S17", tb, None), "1d", 24 * HOUR_MS) == 20 * 24 * HOUR_MS
    assert vsp.own_hold("S12", _sig("S12", tb, None), "4h", tb.bar_ms) == vsp.HOLD["4h"]


def test_s23_limit_cancelled_by_the_target_before_the_fill_others_not():
    h1 = _h1({H_FILL: (100.5, 102.5), H_FILL + 1: (98.9, 100.0)})  # target first, then a fill
    rows, _, _ = _run("S23", h1, float(ss.S23_HOLD_MS))
    assert len(rows) == 1 and rows[0]["filled"] is False and rows[0]["B_x"] == int(rows[0]["t_in"]) + 4 * HOUR_MS
    rows, _, _ = _run("S17", h1, None)  # no cancel rule: the later bar fills
    assert rows[0]["filled"] is True and rows[0]["t_fill"] == int(h1.t[H_FILL + 1])


@pytest.mark.parametrize(("setup", "hold_ms", "want"), [("S23", 10 * HOUR_MS, 10 * HOUR_MS), ("S17", None, 80 * HOUR_MS)])
def test_filled_trade_held_for_the_signal_hold_and_demo_hold_row(setup, hold_ms, want):
    h1 = _h1({H_FILL: (98.9, 100.0)})  # fill in the first hour, then flat: a time exit
    rows, _, _ = _run(setup, h1, None if hold_ms is None else float(hold_ms))
    r = rows[0]
    assert r["filled"] and r["B_k"] == "time" and r["B_x"] == int(h1.t[H_FILL]) + want
    assert r["D"] is not None and r["K"] is not None and r["K_c"] > 0  # demo-hold row and the known-number field


def test_placebo_target_in_percent_for_s23_and_2r_for_s17():
    h1 = _h1({})
    _, _, tg23 = _run("S23", h1, float(ss.S23_HOLD_MS))
    _, _, tg17 = _run("S17", h1, None)
    # every placebo close is 100 like the signal's, so the S23 placebo keeps the target 102; S17's is the 2R target 103
    assert len(tg23) == 3 and tg23[:2] == pytest.approx([TARGET, TARGET])
    assert tg17[:2] == pytest.approx([LEVEL + 2 * (LEVEL - STOP)] * 2)
