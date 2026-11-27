"""Tests of analytics/s12_demo_audit.py (S12 demo vs passport, IDEA-20261030-demo-setups-depth items 6 and 9)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradedesk.analytics import s12_demo_audit as m
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.setup_signals import LiveSignal
from tradedesk.analytics.stop_management import MAKER, SLIPPAGE, TAKER

H = 3_600_000
T4 = 4 * H
NOW = int(datetime(2026, 11, 9, 18, tzinfo=UTC).timestamp() * 1000)


def _ms(mo: int, d: int, h: int = 0) -> int:
    return int(datetime(2026, mo, d, h, tzinfo=UTC).timestamp() * 1000)


def test_traded_version_sources() -> None:
    assert m.traded_version({"setup": "S12", "version": "v9", "opened_ts": _ms(11, 2)}, now_ms=NOW) == ("v9", "column")
    assert m.traded_version({"setup": "S12", "version": None, "rest_until": _ms(11, 1), "opened_ts": _ms(10, 28, 19)}, now_ms=NOW) == (
        "v7",
        "rest_until",
    )
    # opened 26.10, after v4 (25.10 14:30) and before v7 (27.10 18:00), no resting limit
    assert m.traded_version({"setup": "S12", "version": None, "rest_until": None, "opened_ts": _ms(10, 26, 8)}, now_ms=NOW) == (
        "v4",
        "open_time",
    )
    assert m.traded_version({"setup": "S12", "opened_ts": None}, now_ms=NOW) == ("unknown", "none")


def test_traded_version_untagged_without_resting_limit_in_v7_epoch_is_v4() -> None:
    # opened 28.10, inside the v7 epoch (27.10 18:00 .. 31.10 09:30), but no resting limit: v7 not yet deployed -> v4 entry
    row = {"setup": "S12", "version": None, "rest_until": None, "opened_ts": _ms(10, 28, 19)}
    assert m.traded_version(row, now_ms=NOW) == ("v4", "open_time")
    # the same trade with a resting limit is v7
    assert m.traded_version({**row, "rest_until": _ms(11, 1)}, now_ms=NOW) == ("v7", "rest_until")


def test_passport_map_and_bands() -> None:
    assert m.PASSPORT_OF["v9"] == m.PASSPORT_OF["v4"] == "v4"
    p = m.PASSPORTS["v4"]
    assert m.band_verdict(0.0, p).startswith("✓")
    assert m.band_verdict(0.6, p).startswith("≈")
    assert m.band_verdict(-0.9, p).startswith("✗")
    lo, hi = m.scaled_band(p, 30)
    assert lo == pytest.approx(p.band_iid[0]) and hi == pytest.approx(p.band_iid[2])
    lo26, hi26 = m.scaled_band(p, 26)
    assert lo26 < lo and hi26 > hi  # fewer trades: a wider band


def test_exec_costs_target_long() -> None:
    risk = 10 * (5 + TAKER * 195)
    row = {"side": "long", "qty": 10, "risk_usd": risk, "fees_usd": 0.5, "fill": 100.0, "entry": 100.0, "stop": 95.0}
    row |= {"target": 110.0, "exit": 110.0, "exit_reason": "target", "entry_kind": "maker"}
    c = m.exec_costs(row)
    assert c["fee_model_r"] == pytest.approx(10 * MAKER * (100 + 110) / risk)
    assert c["fee_entry_kind_r"] == pytest.approx(c["fee_model_r"])
    assert c["fee_demo_r"] == pytest.approx(0.5 / risk)
    assert c["fee_diff_r"] == pytest.approx(c["fee_model_r"] - 0.5 / risk)
    assert c["exit_slip_r"] == pytest.approx(0.0) and c["model_slip_r"] == 0.0


def test_exec_costs_stop_short_taker_entry() -> None:
    risk = 2 * (5 + TAKER * 205)
    row = {"side": "short", "qty": 2, "risk_usd": risk, "fees_usd": None, "fill": 100.0, "entry": 100.0, "stop": 105.0}
    row |= {"target": 90.0, "exit": 105.2, "exit_reason": "stop", "entry_kind": "taker"}
    c = m.exec_costs(row)
    assert c["fee_model_r"] == pytest.approx(2 * (MAKER * 100 + TAKER * 105.2) / risk)
    assert c["fee_entry_kind_r"] == pytest.approx(2 * (TAKER * 100 + TAKER * 105.2) / risk)
    assert c["fee_demo_r"] is None and c["fee_diff_r"] is None
    assert c["exit_slip_r"] == pytest.approx(-0.2 * 2 / risk)  # a short stopped above its stop: worse
    assert c["model_slip_r"] == pytest.approx(-SLIPPAGE * 105.2 * 2 / risk)
    assert m.exec_costs({"side": "long", "qty": None})["fee_model_r"] is None
    assert m.exec_costs({**row, "exit_reason": "time"})["exit_slip_r"] is None


# --- v7 replay -------------------------------------------------------------------------------------------------------

SIG = _ms(10, 28, 12)
UNTIL = SIG + T4 + 20 * T4


def _bars4() -> Bars:
    return Bars.from_rows([[SIG - 2 * T4, 100, 101, 99, 100], [SIG - T4, 100, 101, 99, 100], [SIG, 100, 101, 99, 100]], T4)


def _path(rows: list[tuple[float, float]], start: int | None = None) -> Bars:
    """1h bars from the signal close; each (low, high), open / close at the middle."""
    t0 = SIG + T4 if start is None else start
    return Bars.from_rows([[t0 + i * H, (lo + hi) / 2, hi, lo, (lo + hi) / 2] for i, (lo, hi) in enumerate(rows)], H)


def _patch(monkeypatch: pytest.MonkeyPatch, side: str, entry: float, stop: float, target: float) -> None:
    sig = LiveSignal("S12", side, "4h", SIG, entry, stop, target, "", {"rest_until_ms": float(UNTIL)})
    monkeypatch.setattr(m, "s12_rest_all", lambda b, tf, lo=0: [sig] if int(b.t[-1]) == SIG else [])


def _row(side: str, entry: float, stop: float, target: float) -> dict:
    return {
        "setup": "S12",
        "timeframe": "4h",
        "side": side,
        "signal_ts": SIG,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rest_until": UNTIL,
    }


def test_v7_replay_long_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "long", 100.0, 95.0, 110.0)
    path = _path([(101, 102), (99.9, 101), (104, 111)])
    r = m.v7_replay(_row("long", 100.0, 95.0, 110.0), _bars4(), path, now_ms=NOW, hold_ms=7 * 24 * H)
    assert r["verdict"] == "replayed" and r["bt_exit"] == "target"
    assert r["fill_bar_ms"] == SIG + T4 + H
    ru = 5 + TAKER * 195
    assert r["r_bt"] == pytest.approx((10 - MAKER * 100 - MAKER * 110) / ru)
    assert r["entry_diff_r"] == 0 and r["rest_until_diff_h"] == 0


def test_v7_replay_short_mirror_and_fill_hour_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "short", 100.0, 105.0, 90.0)
    # the fill bar trades through 100 and reaches the stop 105: a loss at the stop (pessimistic fill hour)
    path = _path([(98, 99), (99, 105.5), (80, 85)])
    r = m.v7_replay(_row("short", 100.0, 105.0, 90.0), _bars4(), path, now_ms=NOW, hold_ms=7 * 24 * H)
    assert r["verdict"] == "replayed" and r["bt_exit"].startswith("stop")
    ru = 5 + TAKER * 205
    assert r["r_bt"] == pytest.approx((-5 - MAKER * 100 - (TAKER + SLIPPAGE) * 105) / ru)


def test_v7_replay_not_filled_and_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "long", 100.0, 95.0, 110.0)
    path = _path([(100, 102)] * 5)  # touches 100 but never trades through it
    assert m.v7_replay(_row("long", 100.0, 95.0, 110.0), _bars4(), path, now_ms=NOW, hold_ms=7 * 24 * H)["verdict"] == "not_filled"
    assert m.v7_replay(_row("short", 100.0, 105.0, 90.0), _bars4(), path, now_ms=NOW, hold_ms=7 * 24 * H)["verdict"] == "no_signal"


def test_v7_replay_ignores_bars_after_the_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """No look-ahead: changing the path after the exit, or adding bars after now_ms, leaves the result unchanged."""
    _patch(monkeypatch, "long", 100.0, 95.0, 110.0)
    base = [(101, 102), (99.9, 101), (104, 111)]
    a = m.v7_replay(_row("long", 100.0, 95.0, 110.0), _bars4(), _path(base + [(50, 60)] * 3), now_ms=NOW, hold_ms=7 * 24 * H)
    b = m.v7_replay(_row("long", 100.0, 95.0, 110.0), _bars4(), _path(base + [(200, 300)] * 3), now_ms=NOW, hold_ms=7 * 24 * H)
    assert a == b
    # a trade still open at now_ms is not resolved from bars that close after it
    now = SIG + T4 + 3 * H
    c = m.v7_replay(
        _row("long", 100.0, 95.0, 110.0), _bars4(), _path([(101, 102), (99.9, 101), (104, 106), (104, 111)]), now_ms=now, hold_ms=7 * 24 * H
    )
    assert c["verdict"] == "no_data"


def test_contrast_ci_point_and_cluster_resampling() -> None:
    # every cluster holds one A trade at 1.0 and one B trade at 0.0: any resample gives exactly +1.0
    d, ci = m.contrast_ci([1.0, 0.0, 1.0, 0.0, 1.0, 0.0], [True, False] * 3, ["d1", "d1", "d2", "d2", "d3", "d3"], n_boot=200, seed=1)
    assert d == pytest.approx(1.0) and ci == pytest.approx((1.0, 1.0))
    # point estimate is mean(A) - mean(B); the CI brackets it and is reproducible with the seed
    v, g, c = [2.0, -1.0, -1.0, 0.5, -1.0, 2.0], [True, True, False, False, True, False], ["a", "b", "c", "a", "b", "c"]
    d2, ci2 = m.contrast_ci(v, g, c, n_boot=500, seed=7)
    assert d2 == pytest.approx(0.0 - 0.5)
    assert ci2 is not None and ci2[0] <= d2 <= ci2[1] and ci2 == m.contrast_ci(v, g, c, n_boot=500, seed=7)[1]
    assert m.contrast_ci([1.0, 2.0], [True, True], ["a", "b"])[1] is None  # group B empty
    assert m.contrast_ci([1.0, 2.0], [True, False], ["a", "a"]) == (pytest.approx(-1.0), None)  # one cluster


def _closed(i: int, closed_ts: int, version: str | None = "v9", **kw) -> dict:
    """A closed S12 row opened 1 h before its close; v9 by its tag unless ``version`` is None (epoch / rest_until)."""
    return {
        "link_id": f"L{i:03d}", "setup": "S12", "status": "closed", "version": version, "rest_until": None,
        "opened_ts": closed_ts - H, "closed_ts": closed_ts, "r_multiple": 0.1 * (i % 7) - 0.3, **kw,
    }  # fmt: skip


def test_first_n_entry_v4_is_ordered_by_close_with_a_total_tie_break_and_ignores_later_rows() -> None:
    now = _ms(11, 27, 12)
    t0 = _ms(11, 1)
    rows = [_closed(i, t0 + i * H) for i in range(1, 120)]
    rows.append(_closed(500, t0 + 5 * H, version="v7"))  # another entry rule: never in the sample
    rows.append({**_closed(501, t0 + 6 * H), "status": "open"})  # not closed
    rows.append({**_closed(502, t0 + 7 * H), "r_multiple": None})  # no result
    rows.append(_closed(503, t0 + 2 * H, version=None, opened_ts=_ms(10, 26, 8)))  # untagged v4 by the open-time epoch
    pick = m.first_n_entry_v4(rows, 100, now_ms=now)
    assert len(pick) == 100
    ids = [r["link_id"] for r in pick]
    assert "L500" not in ids and "L501" not in ids and "L502" not in ids and "L503" in ids
    # the untagged v4 closed at t0 + 2h, opened earlier than L002 -> it sorts first among the two
    assert ids[:3] == ["L001", "L503", "L002"]
    assert ids[-1] == "L099" and [r["closed_ts"] for r in pick] == sorted(r["closed_ts"] for r in pick)
    # a tie at the 100th close minute is broken by the open time, then the link id
    tie_a = _closed(900, t0 + 99 * H, opened_ts=t0 + 99 * H - 2 * H)
    tie_b = _closed(901, t0 + 99 * H, opened_ts=t0 + 99 * H - 2 * H)
    pick2 = m.first_n_entry_v4([*rows, tie_b, tie_a], 100, now_ms=now)
    ids2 = [r["link_id"] for r in pick2]  # L001, L503, L002..L098 = 99, then the earliest-opened, lowest id at 99 h
    assert ids2[-2:] == ["L098", "L900"] and "L901" not in ids2 and "L099" not in ids2
    # trades closed after the 100th, or a change of their R, never change the sample
    later = [_closed(i, t0 + (200 + i) * H) for i in range(30)]
    changed = [{**r, "r_multiple": 9.0} if r["closed_ts"] > pick[-1]["closed_ts"] else r for r in rows]
    assert m.first_n_entry_v4([*changed, *later], 100, now_ms=now) == pick
    assert len(m.first_n_entry_v4(rows[:10], 100, now_ms=now)) == 10  # fewer than n: all of them


def test_review_and_stop_decisions_use_strict_inequalities_at_the_frozen_floors() -> None:
    assert m.DROP_FLOOR == -0.524 and m.STOP_FLOOR == -0.827 and m.REVIEW_N == 100 and m.STOP_N == 30
    assert m.review_decision(-0.6, 99) == "рано"
    assert m.review_decision(-0.524, 100) == "тримати"  # equal to the floor: not below it
    assert m.review_decision(-0.5241, 100) == "зняти"
    assert m.review_decision(+0.3, 100) == "тримати"
    assert m.stop_decision(-0.827, 30) == "тримати" and m.stop_decision(-0.83, 30).startswith("зупинити")
    assert m.stop_decision(-2.0, 29) == "рано"
    assert m.band_verdict_n100(0.259).startswith("✓") and m.band_verdict_n100(-0.30).startswith("≈")
    assert m.band_verdict_n100(-0.524).startswith("≈") and m.band_verdict_n100(-0.53).startswith("✗")
    assert m.band_verdict_n100(0.66).startswith("✗")


def test_r_variants_follow_the_26_11_report() -> None:
    base = {"status": "closed", "side": "long", "fill": 100.0, "qty": 1.0, "fees_usd": 0.0, "risk_usd": 10.0}
    ok = {**base, "link_id": "a", "exit": 110.0, "pnl_usd": 10.0, "r_multiple": 1.0}  # P&L = price P&L
    off = {**base, "link_id": "b", "exit": 90.0, "pnl_usd": 5.0, "r_multiple": 0.5}  # price says −10 USD: flagged
    v, flagged = m.r_variants([ok, off])
    assert flagged == ["b"]
    assert [x for _, x in v["журнал"]] == [1.0, 0.5]
    assert [x for _, x in v["з цін"]] == [1.0, pytest.approx(-1.0)]
    assert [x for _, x in v["без позначених"]] == [1.0]
