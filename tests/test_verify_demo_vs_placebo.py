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
    }


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
