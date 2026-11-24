"""scripts/verify_pm_v2g_na.py: causes of 'n/a' at +24 h (standing item 454), timestamps only, no network."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from verify_pm_v2g_na import NEED_S, account, bucket, early_reason, has_gap, na_cause, render  # noqa: E402

from tradedesk.analytics.pm_gap_paper import HOUR_S, EntryRule  # noqa: E402

END = 2_000_000_000
HOURLY = list(range(END - 100 * HOUR_S, END + 1, HOUR_S))  # a collector with no hole


def _pos(hours_before_end: float, kind: str = "above", mid: str = "m1") -> dict:
    return {"market_id": mid, "side": "yes", "ts": int(END - hours_before_end * HOUR_S), "end_ts": END, "kind": kind}


def test_need_is_26_hours():
    assert NEED_S == 26 * HOUR_S


def test_buckets():
    assert [bucket(h) for h in (0.5, 6, 11.9, 12, 23.99, 24, 25.9, 26, 47.9, 48, 300)] == [
        "<6", "6-12", "6-12", "12-24", "12-24", "24-26", "24-26", "26-48", "26-48", ">=48", ">=48",
    ]  # fmt: skip


def test_has_gap_edges_and_inside():
    t = [0, 3600, 7200, 10800]
    assert not has_gap(t, 0, 10800)
    assert has_gap([0, 3600, 10800], 0, 10800)  # 2 h step inside
    assert has_gap(t, -6000, 10800)  # first moment > 90 min after lo
    assert not has_gap(t, -5000, 10800)  # 83 min: under the bar
    assert not has_gap(t, 5, 5)


def test_na_cause_listed_late_vs_late_entry():
    p = _pos(5)
    assert na_cause(p, [END - 20 * HOUR_S, END - 5 * HOUR_S], HOURLY) == "listed_late"
    assert na_cause(p, [END - 26 * HOUR_S, END - 5 * HOUR_S], HOURLY) == "late_entry"  # exactly 26 h: seen in time
    assert na_cause(p, [], HOURLY) == "rows_missing"


def test_na_cause_listed_late_with_collector_hole():
    hole = [t for t in HOURLY if not END - 25 * HOUR_S <= t <= END - 22 * HOUR_S]
    p = _pos(5)
    assert na_cause(p, [END - 20 * HOUR_S], hole) == "listed_late_gap"
    assert na_cause(p, [END - 20 * HOUR_S], HOURLY) == "listed_late"


def test_cause_unchanged_by_later_rows():
    """Causal: market rows after the entry do not change the cause of an 'n/a'."""
    p = _pos(5)
    base = [END - 20 * HOUR_S, END - 5 * HOUR_S]
    assert na_cause(p, base, HOURLY) == na_cause(p, [*base, END - 2 * HOUR_S, END - HOUR_S], HOURLY)


def test_account_counts_na_and_pending_without_prices():
    pos = [_pos(5, "touch", "a"), _pos(10, "above", "b"), _pos(30, "above", "c")]
    by_mkt = {
        "a": [{"ts": END - 20 * HOUR_S}, {"ts": END - 5 * HOUR_S}],
        "b": [{"ts": END - 60 * HOUR_S}, {"ts": END - 10 * HOUR_S}],
        "c": [{"ts": END - 60 * HOUR_S}],
    }
    now = END - 29 * HOUR_S  # c's +24 h point (entry + 25 h with tolerance) still ahead
    a = account(pos, by_mkt, HOURLY, now)
    assert a["n"] == 3
    assert a["status"] == {"n/a": 2, "pending": 1}
    assert a["na_cause"] == {"listed_late": 1, "late_entry": 1}
    assert sum(a["na_cause"].values()) == a["status"]["n/a"]
    assert a["na_by_kind"] == {"above": 1, "touch": 1}  # the pending 'above' is not split by kind
    assert a["na_entry_to_end_h"] == {"<6": 1, "6-12": 1}
    assert a["na_cause_by_entry_h"] == {"listed_late/<6": 1, "late_entry/6-12": 1}
    assert a["na_listed_before_end_h"] == {"12-24": 1, ">=48": 1}
    assert a["late_entry_early_reason"] == {}  # no period rows given


def _leaves(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _leaves(v)
    else:
        yield x


def test_account_hides_clv_and_splits_only_na_by_kind():
    """Embargo (review 454, items 1-2): an 'ok' point's CLV is discarded, and a touch 'ok' / 'missing' does not show up
    in the by-kind split (a touch 'missing' usually means the level was hit)."""
    ok, miss, na = (_pos(30, "touch", "o") | {"cost": 0.3}, _pos(30, "touch", "m") | {"cost": 0.3}, _pos(5, "touch", "a"))
    by_mkt = {
        "o": [
            {"ts": END - 30 * HOUR_S, "yes_bid": 0.6713, "yes_ask": 0.6913},
            {"ts": END - 6 * HOUR_S, "yes_bid": 0.6713, "yes_ask": 0.6913},
        ],
        "m": [{"ts": END - 30 * HOUR_S, "yes_bid": 0.6713, "yes_ask": 0.6913}],  # no row near +24 h
        "a": [{"ts": END - 20 * HOUR_S}, {"ts": END - 5 * HOUR_S}],
    }
    a = account([ok, miss, na], by_mkt, HOURLY, END)
    assert a["status"] == {"ok": 1, "missing": 1, "n/a": 1}
    assert a["na_by_kind"] == account([na], by_mkt, HOURLY, END)["na_by_kind"] == {"touch": 1}
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in _leaves(a))  # counters only, no CLV / price
    text = "\n".join(render("v2-g", a))
    assert "missing 1" in text and "touch 1" in text
    assert not any(s in text for s in ("0.38", "0.68", "3813", "6813", "0.3"))  # CLV 0.3813, mid 0.6813, cost 0.3


def test_early_reason_labels():
    rule, p = EntryRule(), _pos(5)
    assert early_reason(p, [], rule, set()) == "no_early_rows_in_period"
    late_only = [{"ts": END - 10 * HOUR_S, "end_ts": END, "model_note": "ok"}]
    assert early_reason(p, late_only, rule, set()) == "no_early_rows_in_period"
    early = [
        {"ts": END - 40 * HOUR_S, "end_ts": END, "model_note": "pre-window"},
        {"ts": END - 30 * HOUR_S, "end_ts": END, "model_note": "pre-window"},
        {"ts": END - 28 * HOUR_S, "end_ts": END, "model_note": "ok", "model_prob": 0.5, "model_covered": 1, "rules_hash": "x"},
    ]
    assert early_reason(p, early, rule, set()) == "not_eligible:note:pre-window"
    assert early_reason(p, early[2:], rule, set()) == "not_eligible:rules_not_accepted"
