"""scripts/assistant_paper_report.py (item 284 of docs/ideas/IDEA-20261102-standing-improvements.md): the weekly
counters-only report of the assistant_paper journal on a temp DB built by the production migrations."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tradedesk.analytics import checkpoint as cp
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import assistant_paper_report as script  # noqa: E402

NOW_S = cp._ms(2026, 11, 2, 12) // 1000
COLS = (
    "opened_ts, currency, structure, expiry_ts, spot, forward, t_years, legs, premium_per_btc, entry_fees_per_btc, "
    "max_loss_per_btc, risk_basis, size_btc, deposit_usd, risk_pct, risk_usd, within_risk, status, source, trigger, pnl_usd"
)


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "fixture.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    asyncio.run(go())
    return db


def _rec(opened: int, expiry: int, *, source: str = "method", trigger: str | None = "chain", status: str = "open"):
    return (opened // 1000, expiry // 1000, status, source, trigger)


REC = [
    _rec(cp._ms(2026, 10, 20, 9), cp._ms(2026, 10, 23, 8), status="settled"),
    _rec(cp._ms(2026, 10, 25, 9), cp._ms(2026, 10, 30, 8)),  # 49 h after the previous expiry: one gap; expired, unsettled
    _rec(cp._ms(2026, 10, 30, 9), cp._ms(2026, 12, 25, 8), trigger="regime"),  # standing, inside the last 7 days
    _rec(cp._ms(2026, 10, 21, 9), cp._ms(2026, 10, 24, 8), source="manual", trigger=None),
    _rec(cp._ms(2026, 11, 3, 9), cp._ms(2026, 12, 25, 8)),  # after NOW: not counted
]


def _fill(db: str, tmp_path: Path) -> None:
    con = sqlite3.connect(db)
    for opened, expiry, status, source, trigger in REC:
        con.execute(
            f"INSERT INTO rec_paper ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (opened, "BTC", "long_straddle", expiry, 1, 1, 0.1, "[]", 1, 0, 1, "max_loss", 0.1, 1e4, 1, 100, 1)
            + (status, source, trigger, 12345.678),
        )
    con.commit()
    con.close()
    state = {"date": "2026-11-02", "asof_utc": "2026-11-02T09:05:00Z", "state": "changed", "action": "hold", "rv30": 0.4321}
    (tmp_path / "assistant").mkdir()
    (tmp_path / "assistant" / "paper_state.json").write_text(json.dumps(state), encoding="utf-8")


def test_counters_only_read_only_and_written_to_out(tmp_path, capsys):
    db = _db(tmp_path)
    _fill(db, tmp_path)
    before = Path(db).read_bytes()
    out = tmp_path / "rep" / "assistant_paper.md"
    assert script.main(["--db", db, "--now", str(NOW_S), "--out", str(out), "--print"]) == 0
    text = out.read_text(encoding="utf-8")
    assert Path(db).read_bytes() == before  # nothing written to the DB
    assert text in capsys.readouterr().out
    assert "- угод методу: 3" in text and "- експірованих / з них ще не розраховано: 2 / 1" in text
    assert "- проміжків без чинної угоди методу > 1 доби: 1 (разом 2.0 дн.)" in text
    assert "- нових угод методу за 7 днів: 1" in text  # 30.10 only: the cut is 26.10 12:00; manual and 03.11 not counted
    assert "- останнє денне рішення (файл стану): 2026-11-02, changed / hold, 3 год тому" in text
    assert "12345" not in text and "0.4321" not in text and "pnl" not in text.lower()  # no P&L, no extra state keys
    assert "Помилки читання" not in text


def test_empty_db_reports_zero_counters(tmp_path):
    db = _db(tmp_path)  # the table exists, no rows, no state file
    code, text = script.build(db, NOW_S)
    assert code == 0
    assert "- угод методу: 0" in text and "- нових угод методу за 7 днів: 0" in text
    assert "перша / остання угода методу" not in text and "файлу стану завдання немає" in text


def test_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    db = _db(tmp_path)
    target = tmp_path / "env" / "r.md"
    monkeypatch.setenv(script.ENV_OUT, str(target))
    assert script.main(["--db", db, "--now", str(NOW_S), "--dry-run"]) == 0
    assert "# assistant_paper:" in capsys.readouterr().out and not target.exists()
    assert script.main(["--db", db, "--now", str(NOW_S)]) == 0  # without --out: the env path
    assert target.read_text(encoding="utf-8").startswith("# assistant_paper:")


def test_missing_db_or_table_is_named_never_raises(tmp_path):
    code, text = script.build(str(tmp_path / "absent.sqlite"), NOW_S)
    assert code == 1 and "не відкрито (FileNotFoundError)" in text and "- угод методу: 0" in text
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).close()
    code, text = script.build(str(other), NOW_S)
    assert code == 1 and "rec_paper: таблиці немає" in text
    assert not (tmp_path / "absent.sqlite").exists()  # mode=ro does not create the file


def test_connection_is_read_only(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        script.cr.connect_ro(db).execute("DELETE FROM rec_paper")


def test_telegram_failure_exit_code(tmp_path, monkeypatch):
    import tg_send

    sent = []
    monkeypatch.setattr(tg_send, "send", lambda text, **kw: sent.append((text, kw)) or False)
    db = _db(tmp_path)
    assert script.main(["--db", db, "--now", str(NOW_S), "--out", str(tmp_path / "r.md"), "--telegram"]) == 3
    assert sent and sent[0][1] == {"topic": "agents", "silent": True}
