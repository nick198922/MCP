"""Weekly descriptive report and the one-time verdict of the Polymarket <-> Deribit paper test (registration section 1)."""

from __future__ import annotations

import hashlib
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tradedesk.analytics.pm_gap_paper import EntryRule
from tradedesk.analytics.pm_gap_report import (
    DAY_S,
    T0_TS,
    binance_check,
    binance_closes,
    final_metrics,
    final_metrics_v2g,
    gamma_outcome,
    main_period,
    render_verdict,
    render_verdict_v2g,
    render_weekly,
    render_weekly_v2g,
    settled_outcomes,
    verdict_ts,
    weekly_descriptive,
    weekly_v2g,
)
from tradedesk.analytics.pm_threshold import settle_ts
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pm_gap_paper_report as script  # noqa: E402

H = 3600
END = settle_ts(date(2026, 10, 29))  # 12:00 ET
T0 = END - 100 * H
TEXT = (
    'This market will resolve to "Yes" if the Binance 1 minute candle for BTC/USDT closing at 12:00 in the ET timezone (noon) '
    'on the date specified in the title has a final "Close" price higher than the price specified in the title.'
)
HASH = hashlib.sha256(TEXT.encode()).hexdigest()[:16]
# Entry: YES ask 0.4329 against a model 0.60 (edge ~0.17); from the next hour the YES mid is 0.7371. So every position's
# CLV is 0.7371 - 0.4329 = 0.3042 and its P&L (YES paid) 1 - 0.4329 = 0.5671: values the weekly report must NOT print.
ENTRY = {"yes_bid": 0.4129, "yes_ask": 0.4329, "no_bid": 0.5671, "no_ask": 0.5871}
# later the model agrees with the market (no new entry on either side)
LATER = {"yes_bid": 0.7271, "yes_ask": 0.7471, "no_bid": 0.2529, "no_ask": 0.2729, "model_prob": 0.74}
HIDDEN = ("30.42", "0.3042", "30.4 ", "56.71", "0.5671", "56.7 ")


def row(ts: int, mid: str = "m1", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": ts,
        "market_id": mid,
        "asset": "BTC",
        "kind": "above",
        "direction": "above",
        "strike": 70_000.0,
        "end_ts": END,
        "hours_to_end": (END - ts) / H,
        "yes_ask_usd": 500.0,
        "no_ask_usd": 500.0,
        "fee_rate": 0.0,
        "fee_exp": 1.0,
        "rules": TEXT if ts == T0 else None,
        "rules_hash": HASH,
        "rules_standard": 1,
        "model_prob": 0.60,
        "model_note": "ok",
        "model_iv": 0.50,
        "model_covered": 1,
        "spot": 70_000.0,
        "book_ts": ts,
        "surface_ts": ts + 5,
        **(ENTRY if ts == T0 else LATER),
    }
    base.update(kw)
    return base


def market_rows(mid: str, asset: str = "BTC") -> list[dict[str, Any]]:
    return [row(t, mid, asset=asset) for t in range(T0, END - H + 1, H)]


ROWS = market_rows("m1") + market_rows("m2") + market_rows("m3", "ETH") + market_rows("m4", "ETH")
OUTCOMES = [
    {"market_id": m, "kind": "above", "strike": 70_000.0, "outcome_yes": 1, "binance_close_end": 71_000.0, "binance_close_next": 71_005.0}
    for m in ("m1", "m2", "m3", "m4")
]
FORBIDDEN_KEYS = {"mean", "lo", "hi", "ci", "pnl", "brier", "diff", "se", "won", "verdict", "ret_capital"}


def _keys(x: Any) -> set[str]:
    if isinstance(x, dict):
        return set(x) | {k for v in x.values() for k in _keys(v)}
    if isinstance(x, list):
        return {k for v in x for k in _keys(v)}
    return set()


# ----------------------------------------------------------------------------- settled results


def test_binance_closes_and_check():
    end = END
    bars = [[(end - 120) * 1000, 1, 1, 1, 69_000.0, 1], [(end - 60) * 1000, 1, 1, 1, 70_100.0, 1], [end * 1000, 1, 1, 1, 69_900.0, 1]]
    assert binance_closes(bars, end) == (70_100.0, 69_900.0)
    assert binance_closes([], end) == (None, None)
    assert binance_closes([["bad"]], end) == (None, None)
    assert binance_check(1, 70_000.0, 70_100.0, 70_200.0) == "match"
    assert binance_check(0, 70_000.0, 70_100.0, 70_200.0) == "mismatch"
    assert binance_check(0, 70_000.0, 70_100.0, 69_900.0) == "ambiguous"  # crossed in that minute, Gamma agrees with one
    assert binance_check(1, 70_000.0, 70_000.0, None) == "mismatch"  # strictly higher than
    assert binance_check(1, 70_000.0, None, None) == "unchecked" and binance_check(None, 70_000.0, 1.0, 1.0) == "unchecked"


def test_gamma_outcome_checks_labels_order_and_uma_status():
    m = {"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]', "umaResolutionStatus": "resolved"}
    assert gamma_outcome(m) == 1 and gamma_outcome({**m, "outcomePrices": '["0", "1"]'}) == 0
    assert gamma_outcome({k: v for k, v in m.items() if k != "umaResolutionStatus"}) == 1  # field absent: not required
    assert gamma_outcome({**m, "outcomes": '["No", "Yes"]'}) is None  # prices are read by position: refuse other orders
    assert gamma_outcome({**m, "outcomes": '["Up", "Down"]'}) is None
    assert gamma_outcome({**m, "outcomes": ["Yes", "No"]}) == 1  # a decoded list is fine too
    assert gamma_outcome({**m, "umaResolutionStatus": "proposed"}) is None  # stored once: wait for the final one
    assert gamma_outcome({**m, "closed": False}) is None
    assert gamma_outcome({**m, "outcomePrices": '["0.5", "0.5"]'}) is None


def test_registered_t0_is_the_first_live_snapshot():
    from datetime import UTC, datetime

    assert datetime.fromtimestamp(T0_TS, UTC).isoformat() == "2026-10-20T18:20:35+00:00"


def test_settled_outcomes_exclude_and_list_contradictions():
    rows = [
        {"market_id": "a", "kind": "above", "strike": 100.0, "outcome_yes": 1, "binance_close_end": 101.0, "binance_close_next": 102.0},
        {"market_id": "b", "kind": "above", "strike": 100.0, "outcome_yes": 1, "binance_close_end": 99.0, "binance_close_next": 98.0},
        {"market_id": "c", "kind": "touch", "strike": 100.0, "outcome_yes": 0},
        {"market_id": "d", "kind": "above", "strike": 100.0, "outcome_yes": None},
        {"market_id": "e", "kind": "above", "strike": 100.0, "outcome_yes": 0, "binance_close_end": None, "binance_close_next": None},
    ]
    usable, s = settled_outcomes(rows)
    assert usable == {"a": 1, "c": 0, "e": 0}
    assert s == {"settled": 4, "check": {"match": 1, "mismatch": 1, "unchecked": 1}, "excluded": ["b"]}


# ----------------------------------------------------------------------------- weekly descriptive report


def test_weekly_report_counts_and_never_shows_the_hidden_metrics():
    now = END + 2 * H
    d = weekly_descriptive(ROWS, OUTCOMES, now, T0)
    assert not (_keys(d) & FORBIDDEN_KEYS)
    s, p, src, sd = d["snapshots"], d["positions"], d["sources"], d["sd_per_position"]
    assert s["rows"] == len(ROWS) and s["times"] == 100 and s["markets"] == 4
    assert s["by_asset_kind"] == {"BTC/above": 200, "ETH/above": 200}
    assert s["share_without_read_times"] == 0 and s["dropped_for_skew"] == 0
    assert p["n"] == 4 and p["by_asset_kind_side"] == {"BTC/above/yes": 2, "ETH/above/yes": 2}
    assert p["resolution_days"] == 1 and p["resolution_weeks"] == 1 and p["ended"] == 4 and p["with_settled_result"] == 4
    assert src["h24"] == {"ok": 4} and src["h24_share_missing"] == 0 and src["close"] == {"close": 4}
    assert src["close_shares"] == {"close": 1.0, "late": 0.0, "missing": 0.0}
    assert sd["h24"] == {"n": 4, "sd": pytest.approx(0.0, abs=1e-12)}
    text = render_weekly(d)
    for hidden in HIDDEN:
        assert hidden not in text, hidden
    assert "закриття: close 4" in text and "BTC/above/yes 2" in text


def test_weekly_report_pending_horizons_skew_and_missing_read_times():
    # five hours after T0: +24 h and the closing window are still ahead, so they are pending, not missing
    early = [r for r in ROWS if r["ts"] <= T0 + 5 * H]
    d = weekly_descriptive(early, [], T0 + 5 * H, T0)
    assert d["positions"]["n"] == 4
    assert d["sources"]["h24"] == {"pending": 4} and d["sources"]["h24_share_missing"] is None
    assert d["sources"]["close"] == {"pending": 4} and d["sources"]["close_shares"]["close"] is None
    # skew > 60 s drops a row that would be eligible; rows without the read times pass and are counted
    rows = [dict(r, surface_ts=r["book_ts"] + 61) if r["market_id"] == "m1" else r for r in ROWS]
    rows = [dict(r, book_ts=None) if r["market_id"] == "m2" else r for r in rows]
    d = weekly_descriptive(rows, [], END + 2 * H, T0)
    assert d["snapshots"]["dropped_for_skew"] == 99 and d["snapshots"]["share_dropped_for_skew"] == pytest.approx(0.25)
    assert d["snapshots"]["without_read_times"] == 100 and d["snapshots"]["share_without_read_times"] == pytest.approx(0.25)
    assert d["positions"]["n"] == 3  # m1 never eligible
    # a +24 h point without a snapshot is 'missing' once its time has passed
    holes = [r for r in ROWS if not (T0 + 23 * H <= r["ts"] <= T0 + 25 * H)]
    d = weekly_descriptive(holes, [], END + 2 * H, T0)
    assert d["sources"]["h24"] == {"missing": 4} and d["sources"]["h24_share_missing"] == 1.0


def test_weekly_sd_is_the_spread_of_known_values():
    # per market a different later mid -> different CLVs; the SD is reported, the mean is not
    mids = {"m1": 0.60, "m2": 0.65, "m3": 0.72, "m4": 0.81}
    rows = [
        r
        if r["ts"] == T0
        else dict(
            r,
            yes_bid=(m := mids[r["market_id"]]) - 0.01,
            yes_ask=m + 0.01,
            no_bid=1 - m - 0.01,
            no_ask=1 - m + 0.01,
            model_prob=m,
        )
        for r in ROWS
    ]
    d = weekly_descriptive(rows, [], END + 2 * H, T0)
    assert d["sd_per_position"]["h24"]["sd"] == pytest.approx(statistics.stdev(m - 0.4329 for m in mids.values()), abs=1e-9)
    assert not (_keys(d) & FORBIDDEN_KEYS)


def test_main_period_bounds_positions():
    start, end = main_period(T0)
    assert end - start == 112 * DAY_S and verdict_ts(T0) == T0 + 126 * DAY_S
    late = [dict(r, ts=r["ts"] + 112 * DAY_S, end_ts=END + 112 * DAY_S) for r in market_rows("m9")]
    d = weekly_descriptive(ROWS + late, [], END + 113 * DAY_S, T0)
    assert d["positions"]["n"] == 4  # entries after T0 + 112 days are not in the main test


# ----------------------------------------------------------------------------- the verdict


def test_final_metrics_refuse_before_the_verdict_date_and_decide_after():
    with pytest.raises(ValueError, match="T0 \\+ 126 days"):
        final_metrics(ROWS, OUTCOMES, verdict_ts(T0) - 1, T0)
    f = final_metrics(ROWS, OUTCOMES, verdict_ts(T0), T0)
    assert f["positions"] == 4
    assert f["primary_clv_24h"]["mean"] == pytest.approx(0.7371 - 0.4329)
    assert f["pnl"]["mean"] == pytest.approx(1 - 0.4329)
    assert (f["verdict"], f["reason"]) == ("inconclusive", "min_sample")  # one week of data: never enough
    # a market whose Gamma result contradicts Binance is excluded from every metric
    bad = [dict(OUTCOMES[0], binance_close_end=69_000.0, binance_close_next=69_000.0), *OUTCOMES[1:]]
    g = final_metrics(ROWS, bad, verdict_ts(T0), T0)
    assert g["positions"] == 3 and g["excluded_markets"] == ["m1"]
    assert g["robustness"]["variants"]["threshold_0.03"]["positions"] == 3  # the exclusion holds in every variant


CLV, PNL = 0.7371 - 0.4329, 1 - 0.4329


def test_verdict_stress_and_robustness_sections_6_and_12():
    r = final_metrics(ROWS, OUTCOMES, verdict_ts(T0), T0)["robustness"]
    v = r["variants"]
    # model 0.60 vs ask 0.4329: edge ~16.7 pp passes 3 and 8 pp, each k alone and $500 of depth
    for name in ("threshold_0.03", "threshold_0.08", "k_1.0", "k_0.92", "depth_500"):
        assert v[name]["positions"] == 4, name
        assert v[name]["clv_24h"]["mean"] == pytest.approx(CLV) and v[name]["pnl"]["mean"] == pytest.approx(PNL)
    # one cent worse: same positions, CLV and P&L both 1 cent lower (fee 0 in the fixture)
    assert v["entry_plus_1c"]["positions"] == 4
    assert v["entry_plus_1c"]["clv_24h"]["mean"] == pytest.approx(CLV - 0.01)
    assert v["entry_plus_1c"]["pnl"]["mean"] == pytest.approx(PNL - 0.01)
    # at the next snapshot the market already agrees with the model: every position drops out
    assert (v["next_snapshot"]["positions"], v["next_snapshot"]["dropped"], v["next_snapshot"]["share_dropped"]) == (0, 4, 1.0)
    assert v["next_snapshot"]["clv_24h"]["mean"] is None
    # halves of the main period by entry; by asset; by kind
    assert r["halves"]["first"]["positions"] == 4 and r["halves"]["second"]["positions"] == 0
    assert r["halves"]["split_ts"] == T0 + 56 * DAY_S
    assert {a: b["positions"] for a, b in r["by_asset"].items()} == {"BTC": 2, "ETH": 2}
    assert {k: b["positions"] for k, b in r["by_kind"].items()} == {"above": 4, "touch": 0}
    # identical CLV for both assets: no difference, permutation p = 1
    perm = r["asset_permutation"]
    assert perm["diff"] == pytest.approx(0.0, abs=1e-12) and perm["p"] == pytest.approx(1.0) and (perm["n_a"], perm["n_b"]) == (2, 2)
    # resolution-day clusters and the closing CLV from 'close' only
    assert r["cluster_day"]["clv_close"]["n_clusters"] == 1 and r["cluster_day"]["pnl"]["mean"] == pytest.approx(PNL)
    assert r["cluster_day"]["brier_k1"]["n"] == 4
    assert r["clv_close_only_close"]["n"] == 4 and r["clv_close_only_close"]["mean"] == pytest.approx(CLV)


def test_verdict_stress_delayed_survivors_and_thin_books():
    # m1 keeps the entry book one more hour (its gap survives the next snapshot); m2 has only $300 at the ask
    rows = [dict(r, **ENTRY, model_prob=0.60) if r["market_id"] == "m1" and r["ts"] == T0 + H else r for r in ROWS]
    rows = [dict(r, yes_ask_usd=300.0) if r["market_id"] == "m2" else r for r in rows]
    f = final_metrics(rows, OUTCOMES, verdict_ts(T0), T0)
    v = f["robustness"]["variants"]
    assert f["positions"] == 4 and v["depth_500"]["positions"] == 3
    nxt = v["next_snapshot"]
    assert (nxt["positions"], nxt["dropped"], nxt["share_dropped"]) == (1, 3, 0.75)
    assert nxt["clv_24h"]["mean"] == pytest.approx(CLV)  # entered at T0 + 1 h at the same ask, +24 h from there
    # the primary metric and the verdict are not changed by any of this
    assert f["primary_clv_24h"]["mean"] == pytest.approx(CLV) and f["verdict"] == "inconclusive"
    text = render_verdict(f)
    assert "Стрес і стійкість" in text and "глибина на аску ≥ 500 $: позицій 3" in text
    assert "вхід на наступному знімку (≤ 2 год): позицій 1 (випало 3, 75.0 %)" in text
    assert "перестановка міток активу всередині тижня входу" in text and "без 3 тижнів" in text


# ----------------------------------------------------------------------------- variant v2-g beside v1

G_UP = 0.0014  # Binance 0.14 % above the index (the H8 measurement)


def _with_g(r: dict[str, Any]) -> dict[str, Any]:
    """binance_last only on m1 (BTC) and m3 (ETH), as the collector stores it only on touch rows; m2 / m4 take the g of
    their snapshot (same ts and asset). Entry snapshot T0: BTC +0.14 % (YES edge grows), ETH -3 % (the YES edge rested
    on pricing off the index). Later snapshots: g = 0."""
    m = r["market_id"]
    if m in ("m2", "m4"):
        return dict(r, binance_last=None)
    up = 1 + G_UP if m == "m1" else 0.97
    return dict(r, binance_last=r["spot"] * (up if r["ts"] == T0 else 1.0))


ROWS_G = [_with_g(r) for r in ROWS]


def test_weekly_v2g_counts_split_and_never_shows_the_hidden_metrics():
    d = weekly_v2g(ROWS_G, OUTCOMES, END + 2 * H, T0)
    assert not (_keys(d) & FORBIDDEN_KEYS)
    assert d["model"] == "v2-g" and d["eligible"] == 4 * 99 and d["eligible_without_binance"] == 0
    assert d["g_median"] == 0.0 and d["g_min"] == pytest.approx(-0.03) and d["g_max"] == pytest.approx(G_UP)
    p = d["positions"]
    assert p["n"] == 2 and p["by_asset_kind_side"] == {"BTC/above/yes": 2}  # m2 entered on its snapshot's g
    assert (p["same_as_v1"], p["only_v2g"], p["only_v1"]) == (2, 0, 2)
    assert p["edge_median_v1"] == pytest.approx(0.60 - 0.4329) and p["edge_median"] > p["edge_median_v1"]  # g > 0 lifts YES
    assert d["sd_per_position"]["h24"]["n"] == 2
    assert d["v1_split"] == {
        "status": {"robust": 2, "g_dependent": 2, "no_binance": 0, "not_computed": 0},
        "g_dependent_by_group": {"above:above:yes": 2},
    }
    text = render_weekly_v2g(d)
    for hidden in HIDDEN:
        assert hidden not in text, hidden
    assert "Варіант v2-g" in text and "тримався лише на зсуві g 2 (above:above:yes 2)" in text and "без binance_last 0" in text
    # a snapshot (ts, asset) with no binance_last anywhere: no v2-g reading for its rows, never g = 0
    bare = [dict(r, binance_last=None) if r["asset"] == "ETH" and r["ts"] == T0 else r for r in ROWS_G]
    e = weekly_v2g(bare, OUTCOMES, END + 2 * H, T0)
    assert e["eligible_without_binance"] == 2 and e["v1_split"]["status"]["no_binance"] == 2
    # v1's own weekly report is unchanged by binance_last
    assert weekly_descriptive(ROWS_G, OUTCOMES, END + 2 * H, T0) == weekly_descriptive(ROWS, OUTCOMES, END + 2 * H, T0)


def test_v2g_metrics_only_at_the_verdict_beside_an_unchanged_v1():
    with pytest.raises(ValueError, match="T0 \\+ 126 days"):
        final_metrics_v2g(ROWS_G, OUTCOMES, verdict_ts(T0) - 1, T0)
    f = final_metrics_v2g(ROWS_G, OUTCOMES, verdict_ts(T0), T0)
    assert f["positions"] == 2 and f["edge_mean"] > 0.60 - 0.4329
    assert f["clv_24h"]["mean"] == pytest.approx(CLV) and f["pnl"]["mean"] == pytest.approx(PNL) and f["clv_close"]["n"] == 2
    # Brier over snapshots with a v2-g reading: every market has its snapshot's g
    assert f["brier_diff"]["1.0"]["markets"] == 4 and f["brier_diff"]["0.92"]["markets"] == 4
    s = f["v1_split"]
    assert s["status"]["g_dependent"] == 2 and s["g_dependent"]["positions"] == 2 and s["robust"]["positions"] == 2
    assert s["g_dependent"]["clv_24h"]["mean"] == pytest.approx(CLV)
    # v1 and its verdict do not change
    v1 = final_metrics(ROWS_G, OUTCOMES, verdict_ts(T0), T0)
    assert v1 == final_metrics(ROWS, OUTCOMES, verdict_ts(T0), T0) and v1["positions"] == 4
    text = render_verdict_v2g(f)
    assert "Вердикт виноситься лише за v1" in text and "v2-g: Brier D, k = 1 (ринків 4)" in text
    assert "v1, edge тримався лише на зсуві g: позицій 2" in text


# ----------------------------------------------------------------------------- the runner (read-only DB)


async def _db(path: Path) -> None:
    s = Store(path)
    await s.init()
    try:
        by_ts: dict[int, list[dict[str, Any]]] = {}
        for r in ROWS:
            by_ts.setdefault(r["ts"], []).append(r)
        for ts, rows in by_ts.items():
            await s.insert_pm_gap(ts, rows)
        for o in OUTCOMES:
            await s.upsert_pm_gap_outcome({**o, "asset": "BTC", "end_ts": END}, END + 3 * H)
    finally:
        await s.aclose()


async def test_runner_writes_the_weekly_report_and_refuses_the_early_verdict(tmp_path, capsys):
    db = tmp_path / "db.sqlite"
    await _db(db)
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    out = tmp_path / "paper" / "pm_gap_REPORT.md"
    assert script.main(["--db", str(db), "--out", str(out), "--now", str(END + 2 * H)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "тижневий звіт" in text and "усього 4" in text and "T0 = " in text
    # v2-g rows follow v1; the fixture has no binance_last, so v2-g has no reading (not g = 0)
    assert "Варіант v2-g" in text and "позицій v2-g 0" in text and "без binance_last 4" in text
    for hidden in HIDDEN:
        assert hidden not in text, hidden
    # --verdict today (wall clock before the registered T0 + 126 days): refused, nothing written
    out2 = tmp_path / "verdict.md"
    assert script.main(["--db", str(db), "--out", str(out2), "--verdict"]) == 2
    assert not out2.exists() and "refused" in capsys.readouterr().err
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest  # the DB was only read


def test_verdict_gate_cannot_be_moved_from_the_command_line(tmp_path, capsys):
    db = tmp_path / "db.sqlite"
    out = tmp_path / "verdict.md"
    # a future --now or a past --t0 would put "now" past the gate: both are refused outright, before reading anything
    for flags in (["--now", str(verdict_ts(T0_TS) + 86_400)], ["--t0", "1"], ["--now", "1", "--t0", "1"]):
        assert script.main(["--db", str(db), "--out", str(out), "--verdict", *flags]) == 2
        assert "not allowed" in capsys.readouterr().err and not out.exists()


async def test_verdict_runs_only_on_the_wall_clock_and_the_registered_t0(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    await _db(db)  # first snapshot = T0 of this fixture
    monkeypatch.setattr(script.time, "time", lambda: float(verdict_ts(T0) + 60))
    # the clock is past the fixture's gate, but the DB does not start at the registered T0: refused
    code, text = script.build_verdict(str(db))
    assert code == 2 and "not the registered T0" in text
    monkeypatch.setattr(script, "T0_TS", T0)  # the fixture's registered T0
    code, text = script.build_verdict(str(db))
    assert code == 0 and "Вердикт: inconclusive" in text
    assert text.index("Вердикт: inconclusive") < text.index("Варіант v2-g")
    monkeypatch.setattr(script.time, "time", lambda: float(verdict_ts(T0) - 1))  # one second early
    code, text = script.build_verdict(str(db))
    assert code == 2 and "refused" in text and "Вердикт" not in text


def test_runner_reads_an_old_schema_db_without_migrating_it(tmp_path):
    import sqlite3

    from tradedesk.store.schema import MIGRATIONS

    db = tmp_path / "v17.sqlite"
    con = sqlite3.connect(db)
    for v in sorted(MIGRATIONS):
        if v <= 17:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 17")
    cols = [c for c in script.COLS if c not in ("book_ts", "surface_ts", "model_forward")] + ["hours_to_end"]
    con.executemany(
        f"INSERT INTO pm_gap_snapshot ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [[r.get(c) for c in cols] for r in ROWS]
    )
    con.commit()
    con.close()
    rows, outcomes = script.load(str(db))
    assert len(rows) == len(ROWS) and outcomes == [] and rows[0]["book_ts"] is None
    code, text = script.build_weekly(str(db), END + 2 * H, None)
    assert code == 0 and "без часу читання книги/поверхні: 400 (100.0 %)" in text
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 17
    con.close()


def test_runner_on_an_empty_db(tmp_path):
    db = tmp_path / "empty.sqlite"
    import sqlite3

    sqlite3.connect(db).close()
    assert script.build_weekly(str(db), END, None)[0] == 1
    assert EntryRule().threshold == 0.05  # the registered rule is used unchanged
