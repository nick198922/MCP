"""Direct tests of pure analytics functions behind the numbers and lines of the forward and checkpoint reports that had
no test naming them (item 98 of the standing-improvements card, BACKLOG 6.9; picked by use in the report scripts):

* scripts/pm_gap_paper_report.py -> analytics/pm_gap_report.py: ``horizon_missing_reasons`` (the weekly 'missing'
  breakdown), ``close_early_payout`` (the second closing-CLV variant of the verdict), ``render_robustness`` (the stress /
  robustness lines of the verdict);
* scripts/condor_forward_costs.py -> analytics/condor_forward.py: ``report_keys`` (under ``foreign_keys``, the run-time
  whitelist guard of the part (a) cost report);
* scripts/collector_health.py -> analytics/collector_health.py: ``fmt_dur``, ``fmt_ts`` (every duration and time of the
  collector-health report and brief line);
* scripts/paper_carry_once.py -> collector/carry_paper_engine.py -> analytics/carry_rules.py: ``spread_price_pnl`` and
  ``spread_side_cost`` (price P&L and per-side cost of the carry B paper forward).

Expected values are worked out by hand in the comments (analytical references)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradedesk.analytics.carry_rules import carry_side_cost, spread_open, spread_pnl_now, spread_price_pnl, spread_side_cost
from tradedesk.analytics.collector_health import DAY, HOUR, MINUTE, fmt_dur, fmt_ts
from tradedesk.analytics.condor_forward import REPORT_KEYS, foreign_keys, report_keys
from tradedesk.analytics.pm_gap_report import (
    MISSING_REASONS,
    VARIANT_NAMES,
    close_early_payout,
    horizon_missing_reasons,
    render_robustness,
)

H = 3_600
T = 1_800_000_000  # an arbitrary entry time, unix seconds


# ------------------------------------------------------------------------ pm_gap_report.horizon_missing_reasons


def _pos(mid: str, ts: int = T, *, end_h: int = 72) -> dict:
    return {"market_id": mid, "ts": ts, "end_ts": ts + end_h * H, "side": "yes", "cost": 0.5}


def _snap(mid: str, ts: int, bid: float | None = 0.4, ask: float | None = 0.6) -> dict:
    return {"market_id": mid, "ts": ts, "yes_bid": bid, "yes_ask": ask, "no_bid": None, "no_ask": None}


def test_horizon_missing_reasons_classifies_each_missing_point_once():
    # +24 h point of an entry at T: window [T + 23 h, T + 25 h] (tolerance 1 h); now = T + 48 h, past every window.
    positions = [
        _pos("gap"),  # collector_gap: no snapshot of any market in the window
        _pos("absent", T + 100 * H),  # market_absent: another market's snapshot in its window, none of this one
        _pos("nomid", T + 200 * H),  # no_mid: this market's row in the window, but no bid / ask on either side
        _pos("ok", T + 300 * H),  # a defined point (mid 0.5): not 'missing', not counted
        _pos("na", T + 400 * H, end_h=25),  # entered 25 h < 24 + 2 h before the end: 'n/a', not counted
        _pos("pending", T + 500 * H),  # its window is still open at now: 'pending', not counted
    ]
    rows = [
        _snap("gap", T),
        _snap("gap", T + 26 * H),  # 2 h after the point: outside the window
        _snap("absent", T + 100 * H),
        _snap("other", T + 124 * H),
        _snap("nomid", T + 200 * H),
        _snap("nomid", T + 224 * H, None, None),
        _snap("ok", T + 324 * H),
    ]
    by_mkt: dict[str, list[dict]] = {}
    for r in rows:
        by_mkt.setdefault(r["market_id"], []).append(r)
    times = [r["ts"] for r in reversed(rows)] + [T + 124 * H]  # unsorted, with a duplicate
    # one position at a time, "now" = entry + 48 h; for "pending" now = entry + 24 h (< entry + 25 h, window still open)
    out = {
        p["market_id"]: horizon_missing_reasons([p], by_mkt, times, 24, p["ts"] + (24 if p["market_id"] == "pending" else 48) * H)
        for p in positions
    }
    assert out["gap"] == {"collector_gap": 1, "market_absent": 0, "no_mid": 0}
    assert out["absent"] == {"collector_gap": 0, "market_absent": 1, "no_mid": 0}
    assert out["nomid"] == {"collector_gap": 0, "market_absent": 0, "no_mid": 1}
    for k in ("ok", "na", "pending"):
        assert out[k] == dict.fromkeys(MISSING_REASONS, 0)
    # all together at one late "now": the three missing points, one per reason
    late = T + 600 * H
    assert horizon_missing_reasons(positions[:5], by_mkt, times, 24, late) == {"collector_gap": 1, "market_absent": 1, "no_mid": 1}
    assert list(horizon_missing_reasons([], by_mkt, times, 24, late)) == list(MISSING_REASONS)


def test_horizon_missing_reasons_window_edges_are_inclusive():
    # The collector window is [entry + h - 1 h, entry + h + 1 h], both ends included; one second past either end is a
    # collector gap. The position's own market has no row near the point, so with a snapshot inside it is market_absent.
    p = _pos("m")
    by_mkt = {"m": [_snap("m", T)]}
    now = T + 48 * H

    def reason(t: int) -> str:
        out = horizon_missing_reasons([p], by_mkt, [T, t], 24, now)
        assert sum(out.values()) == 1
        return next(k for k, v in out.items() if v)

    assert reason(T + 23 * H) == "market_absent"
    assert reason(T + 25 * H) == "market_absent"
    assert reason(T + 23 * H - 1) == "collector_gap"
    assert reason(T + 25 * H + 1) == "collector_gap"
    # 6 h horizon: window [T + 5 h, T + 7 h]
    assert horizon_missing_reasons([p], by_mkt, [T + 5 * H], 6, now) == {"collector_gap": 0, "market_absent": 1, "no_mid": 0}


# ------------------------------------------------------------------------ pm_gap_report.close_early_payout

E = T + 72 * H  # every market's end; the closing window ends at E - 1 h


def _pm(mid: str, src: str, clv: float | None, pnl: float | None, day: str) -> dict:
    return {"market_id": mid, "close_src": src, "clv": clv, "pnl": pnl, "end_ts": E, "week_res": "2026-W45", "day": day}


def test_close_early_payout_hand_values():
    pm = [
        _pm("m1", "close", 0.10, 0.40, "d1"),  # closed exactly at E - 1 h with a result: early, value = pnl 0.40
        _pm("m2", "missing", None, -0.30, "d1"),  # closed at E - 2 h with a result: early (and missing), value -0.30
        _pm("m3", "late", 0.05, 0.60, "d2"),  # closed 1 s after E - 1 h: not early, keeps clv 0.05
        _pm("m4", "missing", None, None, "d3"),  # no close time stored, no result: stays missing, without close_ts
        _pm("m5", "missing", None, 0.70, "d3"),  # closed after its window: stays missing (has a close time)
        _pm("m6", "close", -0.02, None, "d4"),  # closed early but no settled result: keeps clv -0.02
    ]
    outcomes = [
        {"market_id": "m1", "closed_ts": E - H},
        {"market_id": "m2", "closed_ts": E - 2 * H},
        {"market_id": "m3", "closed_ts": E - H + 1},
        {"market_id": "m4", "closed_ts": None},
        {"market_id": "m5", "closed_ts": E + 100},
        {"market_id": "m6", "closed_ts": E - 2 * H},
        {"market_id": "m9"},  # an outcome row without the column (before schema 38)
    ]
    out = close_early_payout(pm, outcomes)
    assert out["sources"] == {"close": 2, "late": 1, "missing": 3}
    assert list(out["sources"]) == ["close", "late", "missing"]
    assert out["defined"] == 6
    assert out["early_resolved"] == 2
    assert out["early_resolved_share"] == pytest.approx(2 / 6)
    assert out["early_resolved_missing"] == 1
    assert out["missing_left"] == 2
    assert out["missing_without_close_ts"] == 1
    # second-variant closing CLV: [0.40, -0.30, 0.05, -0.02] -> mean 0.13 / 4 = 0.0325; one resolution week -> no CI
    s = out["clv_close"]
    assert (s["n"], s["n_clusters"], s["n_days"]) == (4, 1, 3)
    assert s["mean"] == pytest.approx(0.0325, abs=1e-12)
    assert s["lo"] is None and s["hi"] is None


def test_close_early_payout_without_close_times_equals_first_variant_and_empty_input():
    pm = [_pm("a", "close", 0.10, 0.40, "d1"), _pm("b", "late", -0.04, 0.0, "d2"), _pm("c", "missing", None, 1.0, "d2")]
    out = close_early_payout(pm, [])  # nothing recorded yet: every position keeps its first-variant closing value
    assert out["early_resolved"] == 0 and out["early_resolved_share"] == 0.0
    assert out["missing_left"] == 1 and out["missing_without_close_ts"] == 1
    assert out["clv_close"]["n"] == 2
    assert out["clv_close"]["mean"] == pytest.approx(0.03, abs=1e-12)  # (0.10 - 0.04) / 2
    empty = close_early_payout([], [{"market_id": "a", "closed_ts": E}])
    assert empty["defined"] == 0 and empty["early_resolved_share"] is None
    assert empty["clv_close"]["n"] == 0 and empty["clv_close"]["mean"] is None


# ------------------------------------------------------------------------ pm_gap_report.render_robustness


def _ci(n: int, k: int, mean: float | None, lo: float | None = None, hi: float | None = None) -> dict:
    return {"n": n, "n_clusters": k, "mean": mean, "lo": lo, "hi": hi}


def _blk(pos: int, clv: dict, pnl: dict | None = None) -> dict:
    return {"positions": pos, "clv_24h": clv, "pnl": pnl or _ci(0, 0, None)}


def _robust(**extra) -> dict:
    a = _ci(12, 10, 0.0123, -0.004, 0.0301)
    b = _ci(4, 3, -0.02, -0.05, 0.01)  # a P&L that differs from CLV, so a swapped or dropped tail is visible
    return {
        "variants": {
            "threshold_0.03": _blk(20, a),
            "next_snapshot": {**_blk(9, a, b), "dropped": 3, "share_dropped": 0.25},
            "custom_variant": _blk(1, _ci(1, 1, 0.5)),
        },
        "halves": {"first": _blk(6, a), "second": _blk(6, a, b), "split_ts": T},
        "by_asset": {"BTC": _blk(7, a), "ETH": _blk(5, a)},
        "by_kind": {"above": _blk(8, a), "touch": _blk(4, a, b)},
        "asset_permutation": {"diff": 0.015, "n_a": 7, "n_b": 5, "p": None, "n_perm": 10_000},
        "cluster_day": {"clv_close": a, "pnl": a, "brier_k1": _ci(0, 0, None)},
        "clv_close_only_close": a,
        **extra,
    }


A_CI = "n 12, кластерів 10; середнє 1.23 п. п., 95 % CI [-0.40 п. п.; 3.01 п. п.]"
NONE_CI = "n 0, кластерів 0; середнє n/a, 95 % CI [n/a; n/a]"
B_CI = "n 4, кластерів 3; середнє -2.00 п. п., 95 % CI [-5.00 п. п.; 1.00 п. п.]"


def test_render_robustness_lines_labels_and_numbers():
    lines = render_robustness(_robust())
    # header + 3 variants + 2 halves + 2 assets + 2 kinds + permutation + cluster-day + only-close + blank + top-3 note
    assert len(lines) == 15
    assert lines[0].startswith("**Стрес і стійкість**")
    assert lines[1] == f"- {VARIANT_NAMES['threshold_0.03']}: позицій 20; CLV +24 год: {A_CI}; P&L: {NONE_CI}"
    assert lines[1].startswith("- поріг 3 п. п.: позицій 20;")
    # next_snapshot carries the dropped count and share; an unknown variant key is printed as is. Rows 2-9 are compared
    # whole, so the CLV / P&L tail of each block is pinned too (b differs from a in three of them).
    assert lines[2] == f"- {VARIANT_NAMES['next_snapshot']}: позицій 9 (випало 3, 25.0 %); CLV +24 год: {A_CI}; P&L: {B_CI}"
    one = "n 1, кластерів 1; середнє 50.00 п. п., 95 % CI [n/a; n/a]"
    assert lines[3] == f"- custom_variant: позицій 1; CLV +24 год: {one}; P&L: {NONE_CI}"
    assert lines[4] == f"- перша половина періоду (вхід): позицій 6; CLV +24 год: {A_CI}; P&L: {NONE_CI}"
    assert lines[5] == f"- друга половина періоду (вхід): позицій 6; CLV +24 год: {A_CI}; P&L: {B_CI}"
    assert lines[6] == f"- BTC: позицій 7; CLV +24 год: {A_CI}; P&L: {NONE_CI}"
    assert lines[7] == f"- ETH: позицій 5; CLV +24 год: {A_CI}; P&L: {NONE_CI}"
    assert lines[8] == f"- пороговий: позицій 8; CLV +24 год: {A_CI}; P&L: {NONE_CI}"
    assert lines[9] == f"- дотик: позицій 4; CLV +24 год: {A_CI}; P&L: {B_CI}"
    assert lines[10] == (
        "- BTC − ETH, CLV +24 год: різниця 1.50 п. п. (n 7 / 5); перестановка міток активу всередині тижня входу: "
        "p = n/a (10000 перестановок)"
    )
    assert lines[11] == f"- кластер = день розв'язання: CLV до закриття {A_CI}; P&L {A_CI}; Brier D (k = 1) {NONE_CI}"
    assert lines[12] == f"- CLV до закриття лише з close: {A_CI}"
    assert lines[13] == ""
    assert lines[14].startswith("Не пораховано: «без 3 тижнів")


def test_render_robustness_permutation_p_and_without_top3_weeks():
    r = _robust(without_top3_weeks={**_ci(12, 10, 0.0123, -0.004, 0.0301), "weeks_removed": ["2026-W44", "2026-W46"]})
    r["asset_permutation"]["p"] = 0.01234
    lines = render_robustness(r)
    assert lines[10].endswith("p = 0.0123 (10000 перестановок)")
    assert lines[14] == f"- без 3 тижнів входу з найбільшою сумою CLV +24 год (2026-W44, 2026-W46): CLV +24 год {A_CI}"
    r["without_top3_weeks"]["weeks_removed"] = []
    assert "(—)" in render_robustness(r)[14]


# ------------------------------------------------------------------------ condor_forward.report_keys


def test_report_keys_collects_keys_at_any_depth():
    x = {"a": 1, "b": {"c": [{"d": 2}, ({"e": 3},)], "f": [1, "x"]}, 5: None}
    # keys of nested dicts inside lists and tuples count; strings inside lists are values, not keys; keys become str
    assert report_keys(x) == {"a", "b", "c", "d", "e", "f", "5"}
    assert report_keys([{"a": 1}, [{"b": {"c": None}}]]) == {"a", "b", "c"}
    for scalar in (None, 1.5, "text", 7):
        assert report_keys(scalar) == set()


def test_report_keys_lets_the_whitelist_guard_see_a_deep_thursday_mark():
    clean = {"currency": "BTC", "entry": {"weeks": [{"friday": "2026-11-06", "actual": 0.1, "model": 0.1, "ratio": 1.0}]}}
    assert report_keys(clean) <= REPORT_KEYS
    assert foreign_keys(clean) == set()
    leaked = {"currency": "BTC", "entry": {"weeks": [{"friday": "2026-11-06", "mark": 61_000.0}]}}
    assert report_keys(leaked) - REPORT_KEYS == {"mark"}
    assert foreign_keys(leaked) == {"mark"}


# ------------------------------------------------------------------------ collector_health.fmt_dur / fmt_ts


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "1 хв"),  # a gap is never shown as 0 minutes
        (29, "1 хв"),
        (45 * MINUTE, "45 хв"),
        (59 * MINUTE + 29, "59 хв"),
        (59 * MINUTE + 30, "1 год"),  # rounds to 60 minutes: shown as an hour, never as '60 хв'
        (HOUR - 1, "1 год"),
        (HOUR, "1 год"),  # from one hour on: whole hours
        (6 * HOUR, "6 год"),
        (47 * HOUR, "47 год"),
        (2 * DAY, "2.0 дн"),  # from two days on: days with one decimal
        (2.5 * DAY, "2.5 дн"),
        (10 * DAY + 3 * HOUR, "10.1 дн"),  # 10.125 days
    ],
)
def test_fmt_dur(seconds, text):
    assert fmt_dur(seconds) == text


def test_fmt_ts_hourly_daily_and_missing():
    ts = int(datetime(2026, 11, 2, 5, 0, tzinfo=UTC).timestamp())
    assert fmt_ts(ts) == "02.11 05:00 UTC"  # default: an hourly series
    assert fmt_ts(ts, MINUTE) == "02.11 05:00 UTC"
    assert fmt_ts(ts, 8 * HOUR) == "02.11 05:00 UTC"  # an 8 h cadence is still shorter than a day
    assert fmt_ts(ts, DAY) == "02.11"  # daily series: the date only
    assert fmt_ts(ts + 59, HOUR) == "02.11 05:00 UTC"  # seconds are not shown
    assert fmt_ts(None) == "—"
    assert fmt_ts(0) == "01.01 00:00 UTC"  # 0 is a time (the epoch), not a missing one
    assert fmt_ts(int(datetime(2026, 12, 31, 23, 59, tzinfo=UTC).timestamp())) == "31.12 23:59 UTC"  # UTC, not local


# ------------------------------------------------------------------------ carry_rules.spread_price_pnl / spread_side_cost


def _spread(d: int, a_e: float, b_e: float, a_last: float, b_last: float) -> dict:
    p = spread_open("X", 0, d, 0.0, a_e, b_e, 0.0)
    p["a_last"], p["b_last"] = a_last, b_last
    return p


def test_spread_price_pnl_hand_values():
    # long A / short B (d = +1), one unit of each, notional priced at A's entry: A 100 -> 110 (+10), B 50 -> 52 (-2 on the
    # short) -> (10 - 2) / 100 = +0.08 of the slot.
    assert spread_price_pnl(_spread(+1, 100.0, 50.0, 110.0, 52.0)) == pytest.approx(0.08, abs=1e-15)
    # the opposite direction loses exactly that
    assert spread_price_pnl(_spread(-1, 100.0, 50.0, 110.0, 52.0)) == pytest.approx(-0.08, abs=1e-15)
    # short A / long B: A 200 -> 190 (+10 on the short), B 205 -> 200 (-5 on the long) -> (10 - 5) / 200 = +0.025
    assert spread_price_pnl(_spread(-1, 200.0, 205.0, 190.0, 200.0)) == pytest.approx(0.025, abs=1e-15)
    # both legs move by the same USD amount: the spread trade is flat whatever the direction
    assert spread_price_pnl(_spread(+1, 100.0, 101.0, 130.0, 131.0)) == pytest.approx(0.0, abs=1e-15)
    assert spread_price_pnl(_spread(+1, 100.0, 50.0, 100.0, 50.0)) == 0.0


def test_spread_side_cost_and_its_place_in_the_open_pnl():
    # two perp legs, taker fee each plus slippage on each: 0.00055 + 0.0005 + 2 x 0.0002 = 0.00145 of notional
    c = spread_side_cost(0.00055, 0.0005, 0.0002)
    assert c == pytest.approx(0.00145, abs=1e-15)
    assert c == pytest.approx(carry_side_cost(0.00055, 0.0005, 0.0002), abs=1e-15)  # same per-side formula as carry A
    assert spread_side_cost(0.0, 0.0, 0.0) == 0.0
    # the open position carries the entry side only: pnl_now = funding + price pnl - one side cost
    p = spread_open("X", 0, +1, 0.0, 100.0, 50.0, c)
    p["a_last"], p["b_last"], p["funding"] = 110.0, 52.0, 0.003
    assert spread_pnl_now(p) == pytest.approx(0.003 + 0.08 - 0.00145, abs=1e-15)
