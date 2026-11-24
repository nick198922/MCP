"""scripts/verify_demo_vs_placebo.py: offline parts (DB loader, pool, deltas, report) on synthetic rows."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_demo_vs_placebo as M  # noqa: E402

DAY = M.DAY_MS


def _pl(r: float | None, status: str = "ok") -> dict:
    return {"coin": "X", "1.0": {"r": r, "status": status}, "1.5": {"r": None if r is None else r - 0.05, "status": status}}


def _row(i: int, setup: str, r_demo: float, pls: list[dict], symbol: str = "AAA/USDT:USDT") -> dict:
    return {
        "link_id": f"l{i}",
        "setup": setup,
        "symbol": symbol,
        "side": "short",
        "tf": "4h",
        "signal_ts": i * DAY,
        "day": i,
        "version": "v5",
        "r_demo": r_demo,
        "status": "ok",
        "sim_signal": {"1.0": {"r": r_demo, "status": "ok"}, "1.5": {"r": r_demo - 0.05, "status": "ok"}},
        "placebo": pls,
        "placebo_ng": pls,
        "pool_n": 97,
        "pool_gold": 0,
    }


def _db(tmp_path: Path, rows: list[tuple], name: str = "w.sqlite") -> str:
    db = tmp_path / name
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE demo_trade (link_id TEXT, setup TEXT, status TEXT, signal_ts INTEGER, closed_ts INTEGER, "
        "timeframe TEXT, version TEXT, r_multiple REAL)"
    )
    con.executemany("INSERT INTO demo_trade VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(db)


def test_deltas_mean_of_available_first_k_and_skip_open() -> None:
    rows = [_row(1, "S38", 1.0, [_pl(0.5), _pl(None, "gap"), _pl(-0.5, "open"), _pl(9.0)])]
    ((d, day, _),) = M.deltas(rows, 3)
    assert d == pytest.approx(1.0) and day == 1  # the 4th placebo (K = 10 only) is not used
    assert M.deltas(rows, 3, skip_open=True) == []
    assert M.deltas(rows, 4)[0][0] == pytest.approx(1.0 - 3.0)
    assert M.deltas(rows, 3, "1.5", sim=True)[0][0] == pytest.approx(0.95 - (0.45 - 0.55) / 2)
    assert M.deltas([{**rows[0], "status": "no_signal_atr"}], 3) == []


def test_pool_excludes_signal_coin_and_uses_previous_day() -> None:
    perps = [("A/USDT:USDT", ""), ("B/USDT:USDT", ""), ("C/USDT:USDT", "innovation")]
    turn = {"A/USDT:USDT": {9 * DAY: 9e6}, "B/USDT:USDT": {9 * DAY: 6e6, 10 * DAY: 1e9}, "C/USDT:USDT": {9 * DAY: 1e6}}
    assert M.pool_for(10 * DAY, perps, turn, "A/USDT:USDT") == ["B/USDT:USDT"]


def test_cochran_and_report_smoke() -> None:
    assert M.cochran({"A": [1.0, 2.0, 3.0]}) is None
    q, df, i2 = M.cochran({"A": [1.0, 2.0, 3.0], "B": [-3.0, -2.0, -1.0]})
    assert df == 1 and q > 0 and 0 <= i2 <= 1
    rows = [_row(i, "S38", (-1) ** i * 1.0, [_pl(0.1 * (i % 3)), _pl(-0.2), _pl(0.3)], f"C{i % 4}/USDT:USDT") for i in range(32)]
    rows += [_row(40 + i, "S39", 0.5, [_pl(0.0)] * 3) for i in range(3)]
    txt = "\n".join(M.report(rows, {"perps": 3}, 100 * DAY))
    assert "S38" in txt and "Вердикти" in txt and "S39 descriptive" in txt and "S40 descriptive" in txt


def test_load_trades_read_only(tmp_path: Path) -> None:
    db = tmp_path / "d.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE demo_trade (link_id TEXT, setup TEXT, status TEXT, signal_ts INTEGER, closed_ts INTEGER)")
    con.executemany(
        "INSERT INTO demo_trade VALUES (?, ?, ?, ?, ?)",
        [("a", "S38", "closed", 1, 5), ("b", "S38", "closed", 1, 50), ("c", "S39", "open", 2, None), ("d", "S3", "closed", 1, 5)],
    )
    con.commit()
    con.close()
    closed, status = M.load_trades(str(db), 10)
    assert [r["link_id"] for r in closed] == ["a"]
    assert status[("S38", "closed")] == 2 and status[("S39", "open")] == 1 and ("S3", "closed") not in status


def test_window_v2_by_signal_ts_and_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "d.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE demo_trade (link_id TEXT, setup TEXT, status TEXT, signal_ts INTEGER, closed_ts INTEGER)")
    con.executemany(
        "INSERT INTO demo_trade VALUES (?, ?, ?, ?, ?)",
        [("a", "S39", "closed", 1, 500), ("b", "S39", "missed", 2, None), ("c", "S39", "open", 20, None), ("d", "S38", "open", 1, None)],
    )
    con.commit()
    con.close()
    rows = M.window_rows(str(db), ("S39",), 10)  # closed long after the boundary still counts: selection is by signal_ts
    assert rows is not None and [r["link_id"] for r in rows] == ["a"]
    assert M.window_rows(str(db), ("S39",), 21) is None and "refused" in capsys.readouterr().out
    future = 4_102_444_800_000  # 2100-01-01 UTC
    assert M.run_v2(str(db), ("S39",), 10, tmp_path, future, tmp_path / "o") == 2  # --now-ms in the future


def test_report_v2_smoke() -> None:
    rows = [
        {**_row(i, "S39", (-1) ** i * 1.0, [_pl(0.1 * (i % 3))] * 10, f"C{i % 4}/USDT:USDT"), "closed_ts_raw": i * DAY} for i in range(32)
    ]
    txt = "\n".join(M.report_v2(rows, {"perps": 3}, 100 * DAY, "test", ("S39",)))
    assert "K main 10" in txt and "S39 ◯" in txt and "t_week" in txt and "family 1" in txt and "без одного дня" in txt
    few = [{**r, "day": r["day"] % 5} for r in rows]  # 5 day clusters: no CI, verdict ◯ by the cluster gate
    txt = "\n".join(M.report_v2(few, {"perps": 3}, 100 * DAY, "test", ("S39",)))
    assert "(< 10 днів: без CI)" in txt and "S39 ◯" in txt
    assert "listed perps at run 3; placebo pool per trade 97 … 97" in txt


def test_report_v2_single_day_leave_one_out() -> None:
    rows = [{**_row(i, "S39", 1.0, [_pl(0.0)] * 10), "day": 7, "closed_ts_raw": 7 * DAY} for i in range(31)]
    txt = "\n".join(M.report_v2(rows, {"perps": 3}, 100 * DAY, "test", ("S39",)))  # must not fail on min() of nothing
    assert "S39 без одного дня: днів 1 — рядка нема" in txt and "S39 ◯" in txt
    two = [{**r, "day": 7 + (i % 2)} for i, r in enumerate(rows)]
    assert "S39 без одного дня: Δ від +1.000 до +1.000" in "\n".join(M.report_v2(two, {"perps": 3}, 100 * DAY, "test", ("S39",)))


def test_report_v2_without_gold_row() -> None:
    rows = []
    for i in range(32):
        r = {**_row(i, "S39", 1.0, [_pl(0.0)] * 10), "closed_ts_raw": i * DAY, "placebo_ng": [_pl(1.0)] * 10}
        rows.append(r)
    rows[0] = {**rows[0], "symbol": "XAUT/USDT:USDT"}  # the gold trade leaves the row
    rows[1] = {**rows[1], "pool_gold": 1}
    rows[2] = {**rows[2], "pool_gold": 2}
    txt = M.report_v2(rows, {"perps": 3}, 100 * DAY, "test", ("S39",))
    (ln,) = [x for x in txt if "без XAUT / PAXG (угоди і пул плацебо)" in x]
    assert "n  31" in ln and "Δ +0.000" in ln  # the no-gold draw, not the main one (main Δ = +1.000)
    assert any(x.startswith(f"  {'S39':<40} n  32") and "Δ +1.000" in x for x in txt)  # the main row keeps it
    assert "S39 XAUT / PAXG: угод з такою монетою сигналу 1; угод, у пулі яких вони були (жереб без них інший) 2" in "\n".join(txt)


def test_evaluate_no_gold_draw_reuses_coins(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    calls: list[str] = []

    async def bars(fetch, sym, tf, sig, hold, now):  # noqa: ANN001, ANN202
        calls.append(sym)
        return sym, None

    monkeypatch.setattr(M, "coin_bars", bars)
    monkeypatch.setattr(M.dp, "atr_at", lambda b, s: 1.0)
    monkeypatch.setattr(M.dp, "placebo_outcome", lambda side, b, *a, **k: M.dp.PlaceboOutcome(float(len(str(b))), "ok"))
    t = {"link_id": "L1", "symbol": "SOL/USDT:USDT", "side": "short", "setup": "S39", "timeframe": "4h", "signal_ts": 10 * DAY}
    t |= {"entry": 100.0, "stop": 101.5, "target": 97.0, "r_multiple": 1.0, "status": "closed", "closed_ts": 11 * DAY}
    coins = [f"C{i:02d}/USDT:USDT" for i in range(20)]
    perps = [(c, "") for c in [*coins, "XAUT/USDT:USDT", "PAXG/USDT:USDT"]]
    turn = {c: {9 * DAY: 1e7} for c, _ in perps}
    row = asyncio.run(M.evaluate(t, perps, turn, None, 20 * DAY, (1.0,)))
    assert row["pool_n"] == 22 and row["pool_gold"] == 2
    ng = [p["coin"] for p in row["placebo_ng"]]
    assert ng == M.draw_coins("L1", coins, 10) and not any(M.dp.is_gold(c) for c in ng)
    assert [p["coin"] for p in row["placebo"]] == M.draw_coins("L1", M.pool_for(10 * DAY, perps, turn, "SOL/USDT:USDT"), 10)
    assert len(calls) == 1 + len({p["coin"] for p in row["placebo"] + row["placebo_ng"]})  # each coin fetched once
    no_gold = [(c, "") for c in coins]
    row2 = asyncio.run(M.evaluate(t, no_gold, turn, None, 20 * DAY, (1.0,)))
    assert row2["pool_gold"] == 0 and row2["placebo_ng"] == row2["placebo"]


def test_counts_prints_counts_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert M.hold_ms("S39", "4h") == 7 * DAY
    db = _db(
        tmp_path,
        [
            ("a", "S39", "closed", DAY, 3 * DAY, "4h", "v5", 1.5),
            ("b", "S39", "closed", 2 * DAY, 4 * DAY, "4h", None, -1.0),
            ("c", "S39", "missed", 2 * DAY, None, "4h", "v5", None),
            ("d", "S39", "open", 30 * DAY, None, "4h", "v5", None),
            ("e", "S38", "closed", DAY, 2 * DAY, "4h", "v5", 2.0),
        ],
    )
    assert M.counts(db, ("S39",), 10 * DAY) == 0
    out = capsys.readouterr().out
    assert "rows of S39: 4" in out and "earliest blocking signal 1970-01-31 00:00 UTC" in out
    assert "closed 2 (r_multiple present 2), other terminal {'missed': 1}, blocking 0" in out
    assert "signal days 2, ISO weeks 1" in out and "version {'v5': 1, 'None': 1}" in out
    assert "placebo horizon ends 1970-01-10 04:00 UTC" in out  # 01-03 00:00 + 4h + 7 d
    assert "1.5" not in out and "-1.0" not in out  # no R printed


def test_run_v2_refuses_before_last_placebo_horizon(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _db(tmp_path, [("a", "S39", "closed", DAY, 3 * DAY, "4h", "v5", 1.0)])
    horizon = DAY + 4 * M.HOUR_MS + 7 * DAY
    assert M.dp.placebo_horizon_end([{"setup": "S39", "timeframe": "4h", "signal_ts": DAY}], M.TF_MS, M.hold_ms) == horizon
    assert M.run_v2(db, ("S39",), 10 * DAY, tmp_path, horizon - 1, tmp_path / "o") == 2
    assert "before the last placebo horizon 1970-01-09 04:00 UTC" in capsys.readouterr().out
    # without --now-ms the wall clock is used: a window whose horizon is still ahead is refused before any fetch
    far = 4_102_444_800_000  # 2100-01-01 UTC
    db2 = _db(tmp_path, [("b", "S39", "closed", far, far + DAY, "4h", "v5", 1.0)], "far.sqlite")
    args = ["--stage", "run", "--db", db2, "--setups", "S39", "--signal-before", "2100-02-01T00:00"]
    assert M.main([*args, "--cache-dir", str(tmp_path / "c"), "--out", str(tmp_path / "o2")]) == 2
    assert "before the last placebo horizon" in capsys.readouterr().out
    assert not (tmp_path / "o2").exists()
