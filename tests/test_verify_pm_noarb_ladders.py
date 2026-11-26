"""Runner of the static no-arbitrage ladders card (scripts/verify_pm_noarb_ladders.py): blindness guard, extract, stages."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_pm_noarb_ladders as M  # noqa: E402

from tradedesk.analytics.pm_threshold import settle_ts  # noqa: E402
from tradedesk.analytics.pm_touch import et_date  # noqa: E402
from tradedesk.store.schema import MIGRATIONS  # noqa: E402

FORBIDDEN = ("model_prob", "model_note", "model_iv", "model_expiry", "expiry_gap_hours", "model_covered", "model_forward", "surface_ts")


def full_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        for sql in MIGRATIONS[v]:
            con.execute(sql)
    con.commit()
    return con


def put(con, ts, mid, strike, *, ya=0.5, na=0.5, yu=100.0, nu=100.0, end=None, bt=None, std=1):
    end = end if end is not None else settle_ts(et_date(ts + 86_400))
    con.execute(
        "INSERT INTO pm_gap_snapshot (ts, market_id, asset, kind, direction, strike, end_ts, hours_to_end, yes_ask, yes_ask_usd, "
        "no_ask, no_ask_usd, fee_rate, fee_exp, rules_hash, rules_standard, book_ts, model_prob, model_note) "
        "VALUES (?, ?, 'BTC', 'above', 'above', ?, ?, 1.0, ?, ?, ?, ?, 0.07, 1.0, 'h', ?, ?, 0.123, 'ok')",
        (ts, mid, strike, end, ya, yu, na, nu, std, ts - 5 if bt is None else bt),
    )


@pytest.fixture
def src(tmp_path):
    con = full_db(tmp_path / "src.sqlite")
    t = M.T_START + 1200
    for i in range(3):
        ts = t + 3600 * i
        put(con, ts, "k90", 90.0, ya=0.40 if i == 1 else 0.60, na=0.62 if i == 1 else 0.5)  # not a crossed book
        put(con, ts, "k92", 92.0, na=0.50, nu=10.0 if i == 2 else 100.0)
    put(con, t, "k94", 94.0, std=0)  # non-standard rules: only in the step-0 count before the rules filter
    put(con, M.T_START - 10, "early", 90.0)
    put(con, M.T_END, "late", 90.0)
    con.execute(
        "INSERT INTO pm_gap_outcome (market_id, asset, kind, strike, end_ts, outcome_yes, checked_ts) VALUES ('k90', 'BTC', 'above', 90, 1, 1, 1)"
    )
    con.commit()
    con.close()
    return tmp_path / "src.sqlite"


def test_whitelist_has_no_model_or_outcome_column():
    assert not set(FORBIDDEN) & set(M.COLUMNS)
    assert not {"path_high", "path_low", "binance_last", "spot", "dvol", "rules", "question"} & set(M.COLUMNS)
    for sql in (M.SQL_ROWS, M.SQL_FP):
        assert "outcome" not in sql and "model" not in sql and "*" not in sql.replace("COUNT(", "")


def test_guard_denies_forbidden_columns_and_tables(src):
    con = M.guard(sqlite3.connect(f"file:{src}?mode=ro", uri=True))
    assert con.execute("SELECT strike, yes_ask FROM pm_gap_snapshot LIMIT 1").fetchone() is not None
    for q in [f"SELECT {c} FROM pm_gap_snapshot" for c in FORBIDDEN] + [
        "SELECT * FROM pm_gap_snapshot",
        "SELECT outcome_yes FROM pm_gap_outcome",
        "SELECT market_id FROM pm_gap_outcome",
        "SELECT ts FROM pm_gap_snapshot WHERE model_prob > 0",
    ]:
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(q).fetchall()


def test_extract_copies_only_the_window_and_whitelist(src, tmp_path):
    dst = tmp_path / "c" / "rows.sqlite"
    fp = M.extract(src, dst)
    assert fp["n_rows"] == 7 and fp["n_ts"] == 3 and len(fp["sha256"]) == 64
    con = sqlite3.connect(dst)
    assert [r[1] for r in con.execute("PRAGMA table_info(pm_gap_snapshot)")] == list(M.COLUMNS)
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == {"pm_gap_snapshot", M.META}
    assert {r[0] for r in con.execute("SELECT market_id FROM pm_gap_snapshot")} == {"k90", "k92", "k94"}
    con.close()
    assert M.fingerprint(M.open_extract(dst)) == fp  # the stages see the same identity as the extract wrote


def test_count_then_violations(src, tmp_path):
    cache = tmp_path / "c"
    M.extract(src, cache / "rows.sqlite")
    cnt = M.stage_count(cache / "rows.sqlite", cache / "count.json")
    a = cnt["families"]["A/all"]
    assert (a["matched"], a["ok"], a["shallow"]) == (3, 2, 1)
    assert cnt["matched_any_rules"]["A"] == 5  # + 90/94 and 92/94 of the non-standard k94 in the first snapshot
    assert cnt["adequacy"]["n_ok_pairs"] == 2 and not cnt["kill_sample_met"]
    assert cnt["effective_window"]["first_ok_ts"] == M.T_START + 1200
    assert cnt["effective_window"]["last_ok_ts"] == M.T_START + 1200 + 3600
    res = M.stage_violations(cache / "rows.sqlite", cache / "count.json", cache / "violations.json")
    assert res["violations_by_family"] == {"A": 1}  # snapshot 2: 0.40 + 0.0168 + 0.50 + 0.0175 = 0.9343
    assert res["violations_by_family_class"] == {"A:sync": 1}  # both legs read at ts - 5
    (ep,) = res["episodes"]
    assert ep["n_snapshots"] == 1 and ep["ended_by"] == "absent" and not ep["survived_next"]  # next NO leg shallow
    assert res["h1_lines"]["headline"]["n_dates"] == 1 and res["h1_lines"]["literal_A_TA"]["n_dates"] == 1
    assert res["verdict"][0] == "inconclusive"
    assert res["floor_gain_usd_counted"]["n"] == 1
    assert res["descriptive"]["A_rev ok"] == 3  # placebo YES(92) + NO(90): its legs are deep in every snapshot
    assert res["descriptive"]["A_rev cost<=0.995"] == 0  # 0.5 + 0.0175 twice = 1.035


def test_h1_rules_are_applied_by_the_runner(tmp_path):
    """Skewed legs count only if the violation survives the check snapshot; a crossed leg never counts."""
    con = full_db(tmp_path / "src.sqlite")
    t = M.T_START + 1200
    end2 = settle_ts(et_date(t + 2 * 86_400))
    for i in range(3):
        ts = t + 3600 * i
        v = i < 2  # ladder 1: a violation in snapshots 0 and 1 with legs read 20 s apart
        put(con, ts, "k90", 90.0, ya=0.40 if v else 0.60, na=0.62)
        put(con, ts, "k92", 92.0, na=0.50, bt=ts - 25)
        put(con, ts, "m90", 90.0, end=end2, ya=0.40 if i == 2 else 0.60, na=0.55)  # ladder 2: crossed own book
        put(con, ts, "m92", 92.0, end=end2, na=0.50)
    con.commit()
    con.close()
    cache = tmp_path / "c"
    M.extract(tmp_path / "src.sqlite", cache / "rows.sqlite")
    M.stage_count(cache / "rows.sqlite", cache / "count.json")
    res = M.stage_violations(cache / "rows.sqlite", cache / "count.json", cache / "violations.json")
    assert res["violations_by_family_class"] == {"A:survived": 1, "A:skew_suspect": 1, "A:crossed": 1}
    d1, d2 = et_date(t + 86_400).isoformat(), et_date(t + 2 * 86_400).isoformat()
    lines = res["h1_lines"]
    assert lines["headline"]["dates"] == [d1]
    assert lines["before_filters"]["dates"] == sorted([d1, d2])
    ep = {e["first_ts"]: e for e in res["episodes"] if e["id"][1] == "k90"}
    assert ep[t]["survived_next"] and ep[t]["n_snapshots"] == 2


def test_violations_stage_refuses_without_count_or_flag(src, tmp_path, monkeypatch):
    cache = tmp_path / "c"
    M.extract(src, cache / "rows.sqlite")
    for argv in (["--stage", "violations"], ["--stage", "violations", "--step1"]):
        monkeypatch.setattr(sys, "argv", ["x", "--cache-dir", str(cache), *argv])
        with pytest.raises(SystemExit, match="refused"):
            M.main()
    M.stage_count(cache / "rows.sqlite", cache / "count.json")
    monkeypatch.setattr(sys, "argv", ["x", "--cache-dir", str(cache), "--stage", "violations"])
    with pytest.raises(SystemExit, match="refused"):
        M.main()
    put_con = sqlite3.connect(cache / "rows.sqlite")
    put_con.execute("DELETE FROM pm_gap_snapshot WHERE market_id = 'k92' AND ts = (SELECT MIN(ts) FROM pm_gap_snapshot)")
    put_con.commit()
    put_con.close()
    with pytest.raises(SystemExit, match="another extract"):
        M.stage_violations(cache / "rows.sqlite", cache / "count.json", cache / "violations.json")


def test_fingerprint_catches_a_changed_value(src, tmp_path):
    cache = tmp_path / "c"
    M.extract(src, cache / "rows.sqlite")
    M.stage_count(cache / "rows.sqlite", cache / "count.json")
    con = sqlite3.connect(cache / "rows.sqlite")
    con.execute("UPDATE pm_gap_snapshot SET no_ask = 0.51 WHERE market_id = 'k92' AND ts = (SELECT MIN(ts) FROM pm_gap_snapshot)")
    con.commit()
    con.close()  # same rows, same ts range: only the content hash differs
    with pytest.raises(SystemExit, match="another extract"):
        M.stage_violations(cache / "rows.sqlite", cache / "count.json", cache / "violations.json")
