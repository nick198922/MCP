"""Stuck demo trades (item 133 of the standing card): the pure rule, its copy of the trader's hold, and the read-only
brief / checkpoint outputs."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradedesk.analytics import checkpoint as cp
from tradedesk.analytics import collector_health as ch
from tradedesk.analytics import stuck_demo as sd
from tradedesk.execution import demo_trader as dt
from tradedesk.store import Store

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import collector_health as script  # noqa: E402

H, D = sd.HOUR_MS, sd.DAY_MS
NOW = int(datetime(2026, 11, 6, 6, 0, tzinfo=UTC).timestamp())  # seconds, the brief's hour
NOW_MS = NOW * 1000


def _row(lid: str, setup: str = "S3", tf: str | None = "4h", *, age_ms: int | None, status: str = "open", **kw):
    opened = None if age_ms is None else NOW_MS - age_ms
    sig = opened - 8 * H if opened is not None else None
    row = {"link_id": lid, "setup": setup, "symbol": f"{lid}/USDT:USDT", "timeframe": tf, "status": status}
    return row | {"signal_ts": sig, "opened_ts": opened} | kw


# ------------------------------------------------------------------------------------------------------ pure


def test_hold_copies_match_the_demo_trader():
    """analytics/ may not import execution/: the copies must stay equal to the trader's rules."""
    assert sd.SETUP_HOLD_MS == dt.SETUP_HOLD_MS and sd.MAX_HOLD_MS == dt.MAX_HOLD_MS
    for setup in (*dt.SETUPS, None, "S99"):
        for tf in ("4h", "1d", "1h", "", None):
            assert sd.hold_ms(setup, tf) == dt.hold_ms(setup, tf), (setup, tf)


def test_stuck_not_stuck_and_closed():
    rows = [
        _row("NEAR", age_ms=10 * D),  # 4h hold 7 d + 1 d grace: stuck
        _row("HYPE", "S16", age_ms=8 * D + H),  # just past 8 d
        _row("EDGE", age_ms=8 * D),  # exactly hold + grace: not yet
        _row("YOUNG", age_ms=2 * D),
        _row("DONE", age_ms=20 * D, status="closed", closed_ts=NOW_MS - 12 * D),
        _row("PEND", age_ms=20 * D, status="pending"),  # not filled: the fill share's business, not this check
        _row("DAY", tf="1d", age_ms=20 * D),  # 1d hold 30 d
        _row("S17", "S17", "1d", age_ms=22 * D),  # its own 20 d hold: past 21 d
    ]
    got = sd.stuck_trades(rows, NOW_MS)
    assert [t["id"] for t in got] == ["S17", "NEAR", "HYPE"]  # oldest first
    near = got[1]
    assert (near["setup"], near["symbol"], near["from_signal"]) == ("S3", "NEAR/USDT:USDT", False)
    assert near["age_h"] == pytest.approx(240) and near["hold_h"] == pytest.approx(168)
    assert got[0]["hold_h"] == pytest.approx(480)


def test_missing_opened_ts_or_timeframe():
    no_tf = _row("NOTF", tf=None, age_ms=9 * D)  # no timeframe: the trader's 7-day default
    assert [t["id"] for t in sd.stuck_trades([no_tf], NOW_MS)] == ["NOTF"]
    assert sd.stuck_trades([_row("NOTF", tf=None, age_ms=7 * D)], NOW_MS) == []
    no_fill = _row("NOFILL", age_ms=None, signal_ts=NOW_MS - 9 * D)  # aged from the signal bar, marked
    (t,) = sd.stuck_trades([no_fill], NOW_MS)
    assert t["from_signal"] and t["age_h"] == pytest.approx(216)
    assert "від сигналу, без часу входу" in sd.describe(t)
    assert sd.stuck_trades([_row("NOFILL", age_ms=None, signal_ts=NOW_MS - 2 * D)], NOW_MS) == []
    assert sd.stuck_trades([_row("NOTIME", age_ms=None)], NOW_MS) == []  # neither time: skipped
    assert sd.stuck_trades([_row("BAD", age_ms=None, opened_ts="x", signal_ts=float("nan"))], NOW_MS) == []


def test_brief_line_only_when_stuck_and_capped():
    assert sd.brief_line([]) is None
    one = sd.stuck_trades([_row("NEAR", age_ms=10 * D)], NOW_MS)
    assert sd.brief_line(one) == "Завислі демо-угоди: 1 (S3 NEAR/USDT:USDT 240 год (строк 168 год))."
    many = sd.stuck_trades([_row(f"C{i}", age_ms=(9 + i) * D) for i in range(6)], NOW_MS)
    line = sd.brief_line(many)
    assert line.startswith("Завислі демо-угоди: 6 (S3 C5/USDT:USDT 336 год") and line.endswith("; і ще 2).")


def test_checkpoint_notes_the_count():
    trades = [_row("NEAR", age_ms=10 * D), _row("OK", age_ms=D), _row("DONE", age_ms=30 * D, status="closed")]
    notes = cp.build({"demo_trades": trades}, NOW_MS)["notes"]
    (note,) = [x for x in notes if x.startswith("завислі демо-угоди")]
    assert ": 1 — S3 NEAR/USDT:USDT;" in note and "n демо неповне" in note
    assert not any("завислі" in x for x in cp.build({"demo_trades": trades[1:]}, NOW_MS)["notes"])
    assert "Примітка: завислі демо-угоди" in cp.render(cp.build({"demo_trades": trades}, NOW_MS))


# ------------------------------------------------------------------------------------------------------ script


@pytest.fixture(autouse=True)
def _quiet_disk_and_snapshot(tmp_path, monkeypatch):
    """Only the demo rows decide the brief here: a roomy fake volume and a fresh assistant snapshot."""
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps({"asof_utc": datetime.fromtimestamp(NOW - 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}), encoding="utf-8")
    monkeypatch.setattr(script, "SNAPSHOT_PATH", snap)
    monkeypatch.setattr(script, "disk_usage", lambda _p: SimpleNamespace(total=100 * ch.GIB, used=40 * ch.GIB, free=60 * ch.GIB))


def _db(tmp_path: Path, rows: list[dict]) -> str:
    db = str(tmp_path / "fixture.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    asyncio.run(go())
    con = sqlite3.connect(db)
    cols = "link_id, venue, symbol, side, setup, timeframe, signal_ts, entry, stop, target, qty, leverage, risk_usd, comment, status, opened_ts"
    for r in rows:
        con.execute(
            f"INSERT INTO demo_trade ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["link_id"], "bybit-demo", r["symbol"], "long", r["setup"], r["timeframe"] or "", r["signal_ts"] or 0, 1, 0.9, 1.2, 1, 5, 100, "c", r["status"], r["opened_ts"]),
        )  # fmt: skip
    con.commit()
    con.close()
    return db


def _quiet_features(tmp_path: Path) -> Path:
    p = tmp_path / "quiet.env"
    p.write_text("\n".join(f"{s.flag}=0" for s in ch.SPECS if s.default_on) + "\n", encoding="utf-8")  # no series
    return p


def test_script_names_stuck_trades_read_only(tmp_path, monkeypatch):
    db = _db(tmp_path, [_row("NEAR", age_ms=10 * D), _row("HYPE", "S16", age_ms=9 * D), _row("NEW", age_ms=D)])
    before = Path(db).read_bytes()
    assert [t["id"] for t in script.stuck_demo_check(db, NOW)] == ["NEAR", "HYPE"]
    line = script.brief_line(db, _quiet_features(tmp_path), NOW)
    assert line.startswith("Завислі демо-угоди: 2 (S3 NEAR/USDT:USDT 240 год (строк 168 год); S16 HYPE/USDT:USDT 216 год")
    out = tmp_path / "r.txt"
    # main reads the wall clock; on the real one NEW (1 d old at NOW) would turn stuck from 2026-11-13 06:00 UTC
    monkeypatch.setattr(script, "time", SimpleNamespace(time=lambda: float(NOW)))
    assert script.main(["--db", db, "--features", str(_quiet_features(tmp_path)), "--out", str(out)]) == 0
    assert "\n✗ Завислі демо-угоди: 2 (" in out.read_text(encoding="utf-8")
    assert Path(db).read_bytes() == before  # nothing written


def test_script_quiet_without_stuck_rows_or_table(tmp_path):
    db = _db(tmp_path, [_row("NEW", age_ms=D), _row("DONE", age_ms=20 * D, status="closed")])
    assert script.stuck_demo_check(db, NOW) == []
    assert script.brief_line(db, _quiet_features(tmp_path), NOW) is None  # N = 0: no line
    bare = tmp_path / "bare.sqlite"
    sqlite3.connect(bare).close()  # no demo_trade table
    assert script.stuck_demo_check(str(bare), NOW) == []
    gone = str(tmp_path / "none.sqlite")  # no DB: only the collectors' line says so
    assert script.brief_line(gone, _quiet_features(tmp_path), NOW) == (
        "Здоров'я колекторів не перевірено: БД не відкрито (FileNotFoundError)."
    )


def test_script_names_a_failed_query(tmp_path, monkeypatch):
    db = _db(tmp_path, [])

    def boom(_db: str, _now: int):
        raise sqlite3.OperationalError("no such column")

    monkeypatch.setattr(script, "stuck_demo_check", boom)
    assert script.brief_line(db, _quiet_features(tmp_path), NOW) == "Завислі демо-угоди не перевірено (OperationalError)."
