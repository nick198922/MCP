"""scripts/verify_pm_h7_qlike.py (item 8l) without network or the collector DB: stage 0 never reads pm_gap_outcome,
and the full stage refuses a stage-0 gate computed on another DB copy before it fetches any test-period bar."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_pm_h7_qlike as M  # noqa: E402

HOUR = 3600
PROF = {(h, w): 3.0 if (h, w) == (10, False) else 1.0 for h in range(24) for w in (False, True)}  # s != 1: a finite gate
MTIME = int(datetime(2026, 11, 4, tzinfo=UTC).timestamp())  # the copy's mtime: cutoff = MTIME - 1 h
DAY0 = int(datetime(2026, 10, 21, tzinfo=UTC).timestamp())  # a Wednesday in W43, after T_START


def _snapshot(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE pm_gap_snapshot (ts INTEGER, market_id TEXT, asset TEXT, kind TEXT, end_ts INTEGER, model_iv REAL, "
        "model_expiry TEXT, expiry_gap_hours REAL, model_note TEXT, model_covered INTEGER)"
    )
    for mid, asset, d, h in (("m1", "BTC", 0, 9), ("m2", "ETH", 1, 11)):  # m1's window holds the heavy hour 10, m2's not
        ts, end = DAY0 + d * 86400 + h * HOUR + 1200, DAY0 + d * 86400 + 16 * HOUR
        expiry = datetime.fromtimestamp(end + 16 * HOUR, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute("INSERT INTO pm_gap_snapshot VALUES (?, ?, ?, 'above', ?, 0.5, ?, 16.0, 'ok', 1)", (ts, mid, asset, end, expiry))
    con.commit()


def _db(path: Path) -> Path:
    con = sqlite3.connect(path)
    _snapshot(con)
    con.execute("CREATE TABLE pm_gap_outcome (market_id TEXT, outcome_yes INTEGER)")  # present, as in a real copy
    con.execute("INSERT INTO pm_gap_outcome VALUES ('m1', 1)")
    con.commit()
    con.close()
    os.utime(path, (MTIME, MTIME))
    return path


@pytest.fixture
def denied(monkeypatch) -> list[str]:
    """Every connection the script opens refuses (and records) any access to pm_gap_outcome."""
    seen: list[str] = []
    real = M.cmn.connect_ro

    def guarded(path, timeout=5):
        con = real(path, timeout)

        def auth(action, arg1, arg2, db_name, trigger):
            if "pm_gap_outcome" in (arg1, arg2):
                seen.append(f"{action}:{arg1}:{arg2}")
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        con.set_authorizer(auth)
        return con

    monkeypatch.setattr(M.cmn, "connect_ro", guarded)
    monkeypatch.setattr(M.VH, "is_profile", lambda: PROF)
    return seen


def test_load_rows_reads_only_the_snapshot_table(tmp_path, denied):
    db = _db(tmp_path / "c.sqlite")
    rows, drop, cutoff = M.load_rows(db, PROF)
    assert denied == []
    con = M.cmn.connect_ro(db)  # the guard itself bites: a read of pm_gap_outcome fails and is recorded
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("SELECT outcome_yes FROM pm_gap_outcome").fetchall()
    con.close()
    assert any("pm_gap_outcome" in x for x in denied)
    assert cutoff == MTIME - HOUR
    assert [(r["mid"], r["asset"]) for r in rows] == [("m1", "BTC"), ("m2", "ETH")]
    assert drop == {}


def test_load_rows_works_on_an_in_memory_db_without_the_outcome_table(tmp_path, monkeypatch):
    """Review 2026-11-01 finding 6 (a): no pm_gap_outcome table at all, so any read of it fails with 'no such table'.
    Stricter than the authorizer guard above: SQLite reports no SQLITE_READ for a JOIN ... USING key, so a filtering
    join on the outcome table slips past `denied` but not past a missing table."""
    mem = sqlite3.connect(":memory:")
    _snapshot(mem)
    assert [t for (t,) in mem.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] == ["pm_gap_snapshot"]
    copy = tmp_path / "copy.sqlite"  # load_rows takes the cutoff from the copy's mtime; the data come from `mem`
    copy.touch()
    os.utime(copy, (MTIME, MTIME))
    opened: list[str] = []

    def connect(path, timeout=5):
        opened.append(str(path))
        return mem

    monkeypatch.setattr(M.cmn, "connect_ro", connect)
    rows, drop, cutoff = M.load_rows(copy, PROF)
    assert opened == [str(copy)]
    assert cutoff == MTIME - HOUR
    assert [(r["mid"], r["asset"]) for r in rows] == [("m1", "BTC"), ("m2", "ETH")]
    assert drop == {}
    probe = sqlite3.connect(":memory:")  # the trap is real: the same schema refuses a read of the outcome table
    _snapshot(probe)
    with pytest.raises(sqlite3.OperationalError, match="no such table: pm_gap_outcome"):
        probe.execute("SELECT outcome_yes FROM pm_gap_outcome").fetchall()
    probe.close()


def _replay_module(poison: float, calls: list[tuple[int, int]]) -> SimpleNamespace:
    """A stand-in for scripts/verify_pm_modules: bar_returns ignores its bounds and also returns bars at and after
    REPLAY_HI (valued `poison`), so only replay's own windowing keeps them out."""

    def bar_returns(closes, lo, hi):
        calls.append((lo, hi))
        out = []
        for i in range((M.REPLAY_HI + 60 * 86400 - M.REPLAY_LO) // HOUR):
            t = M.REPLAY_LO + i * HOUR
            out.append((t, poison if t >= M.REPLAY_HI else 0.001 * (1 + i * 7919 % 13)))
        return out

    return SimpleNamespace(load_btc_long=lambda: [], bar_returns=bar_returns)


def test_replay_never_uses_bars_at_or_after_replay_hi(tmp_path, denied, monkeypatch):
    """Review 2026-11-01 finding 6 (b): poisoning every bar key >= REPLAY_HI with NaN or a huge value changes nothing."""
    assert M.REPLAY_HI <= M.T_START  # the replay cache ends before the test period starts
    rows, _, _ = M.load_rows(_db(tmp_path / "c.sqlite"), PROF)
    s2, _ = M.hq.renormalize([r["s"] for r in rows], M.hq.market_weights([r["mid"] for r in rows]))
    out = {}
    for label, poison in (("plain", 0.002), ("huge", 10.0), ("nan", float("nan"))):
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(M, "_load", lambda name, p=poison, c=calls: _replay_module(p, c))
        out[label] = M.replay(rows, s2)
        assert calls == [(M.REPLAY_LO, M.REPLAY_HI)]
    clean = out["plain"]
    assert clean["rows_scored"] > 0 and math.isfinite(clean["sd_week"]) and math.isfinite(clean["mean_week"])
    k_lo = clean["weeks_back"][0]
    assert max(r["h1"] for r in rows) - k_lo * M.WEEK_S <= M.REPLAY_HI  # the nearest shift ends inside the cache
    assert max(r["h1"] for r in rows) - (k_lo - 1) * M.WEEK_S > M.REPLAY_HI  # and is the nearest one: the edge is tested
    assert out["huge"] == clean
    assert out["nan"] == clean


def test_stale_gate() -> None:
    gate = {"rows": 2, "cutoff": 100, "gate": {"pass": True}}
    assert M.stale_gate(gate, 2, 100) is None
    assert "cutoff 100 != DB copy cutoff 200" in M.stale_gate(gate, 2, 200)
    assert "rows 2 != DB copy rows 3" in M.stale_gate(gate, 3, 100)
    assert "cutoff None" in M.stale_gate({"rows": 2, "gate": {"pass": True}}, 2, 100)  # gate file from before 04.11


def _fake_replay(rows, s2):  # the BTC 1h cache is not needed to write a gate file
    return {"offsets": 1, "weeks_back": [1, 1], "sd_week": 0.01, "mean_week": 0.0, "per_week": {}, "row_mean": 0.0, "rows_scored": 2}


def test_full_stage_refuses_a_gate_from_another_db_copy_before_fetching(tmp_path, denied, monkeypatch):
    db = _db(tmp_path / "c.sqlite")
    monkeypatch.setattr(M, "replay", _fake_replay)
    gate_file = tmp_path / "stage0.json"
    res = M.stage0(db, gate_file)
    assert res["cutoff"] == MTIME - HOUR and res["rows"] == 2
    saved = json.loads(gate_file.read_text())
    assert saved["cutoff"] == MTIME - HOUR
    saved["gate"]["pass"] = True  # pretend the gate passed: only the freshness check is under test
    gate_file.write_text(json.dumps(saved))

    fetched: list[str] = []

    async def no_fetch(out, sym, lo, hi):
        fetched.append(sym)
        raise RuntimeError("fetch reached")

    monkeypatch.setattr(M, "fetch_returns", no_fetch)
    os.utime(db, (MTIME + 86400, MTIME + 86400))  # a refreshed copy: new cutoff, the old gate no longer covers it
    with pytest.raises(SystemExit, match="stale.*cutoff"):
        M.stage_full(db, gate_file, tmp_path / "full.json", 10)
    assert fetched == []

    os.utime(db, (MTIME, MTIME))  # the same copy the gate was computed on: the check passes and the fetch starts
    with pytest.raises(RuntimeError, match="fetch reached"):
        M.stage_full(db, gate_file, tmp_path / "full.json", 10)
    assert fetched == ["BTC/USDT"]
    assert denied == []


def test_full_stage_refuses_a_gate_without_a_cutoff(tmp_path, denied, monkeypatch):
    db = _db(tmp_path / "c.sqlite")
    gate_file = tmp_path / "stage0.json"
    gate_file.write_text(json.dumps({"rows": 2, "gate": {"pass": True}}))  # format before 2026-11-04
    monkeypatch.setattr(M, "fetch_returns", None)  # would fail loudly if reached
    with pytest.raises(SystemExit, match="stale"):
        M.stage_full(db, gate_file, tmp_path / "full.json", 10)
