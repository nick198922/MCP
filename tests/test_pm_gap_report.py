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
    binance_check,
    binance_closes,
    final_metrics,
    main_period,
    render_weekly,
    settled_outcomes,
    verdict_ts,
    weekly_descriptive,
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
    for hidden in HIDDEN:
        assert hidden not in text, hidden
    # --verdict before T0 + 126 days: refused, nothing written
    out2 = tmp_path / "verdict.md"
    assert script.main(["--db", str(db), "--out", str(out2), "--now", str(END + 2 * H), "--verdict"]) == 2
    assert not out2.exists() and "refused" in capsys.readouterr().err
    # at the verdict date it runs (T0 = the first snapshot)
    assert script.main(["--db", str(db), "--out", str(out2), "--now", str(verdict_ts(T0)), "--verdict"]) == 0
    assert "Вердикт: inconclusive" in out2.read_text(encoding="utf-8")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest  # the DB was only read


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
    code, text = script.build(str(db), END + 2 * H, None, False)
    assert code == 0 and "без часу читання книги/поверхні: 400 (100.0 %)" in text
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 17
    con.close()


def test_runner_on_an_empty_db(tmp_path):
    db = tmp_path / "empty.sqlite"
    import sqlite3

    sqlite3.connect(db).close()
    assert script.build(str(db), END, None, False)[0] == 1
    assert EntryRule().threshold == 0.05  # the registered rule is used unchanged
