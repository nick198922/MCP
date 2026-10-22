"""Cancelling a journal trade from the owner's Telegram /cancel command (scripts/journal_cancel_tg.py); temp DB, no network."""

import asyncio
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import journal_cancel_tg as jx  # noqa: E402

from tradedesk.store.db import Store  # noqa: E402

NOW = datetime(2026, 10, 23, 10, tzinfo=UTC).timestamp()
OPENED = int(NOW) - 86400


@pytest.fixture
def journal_db(tmp_path):
    path = tmp_path / "tradedesk.sqlite"

    async def make():
        s = Store(path)
        await s.init()
        await s.aclose()

    asyncio.run(make())
    return path


def add(path, **kw):
    async def go():
        s = Store(path)
        try:
            await s.init()
            return await s.journal_insert(opened_ts=OPENED, **kw)
        finally:
            await s.aclose()

    return asyncio.run(go())


def row(path, trade_id):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM journal WHERE id = ?", (trade_id,)).fetchone())
    finally:
        con.close()


def test_an_open_trade_is_cancelled_like_journal_close_cancelled(journal_db):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0, stop=82000.0, tags=["telegram"])
    out = jx.handle(f"/cancel #{tid} note лімітка не виконалась", journal_db, NOW)
    r = row(journal_db, tid)
    # the columns journal_close(cancelled=true) writes through journal_update_close
    assert (r["status"], r["outcome"], r["closed_ts"]) == ("cancelled", "cancelled", int(NOW))
    assert r["exit"] is None and r["pnl"] is None and r["r_multiple"] is None
    assert r["note"] == "лімітка не виконалась"
    assert (r["entry"], r["stop"], r["size"]) == (84000.0, 82000.0, 0.1)  # the rest of the row is untouched
    assert f"<b>✅ Скасовано в журналі: угода #{tid}</b>" in out and "BTC · лонг · 0.1 · вхід 84 000" in out
    assert "без виходу й P&amp;L" in out and "Нотатка: лімітка не виконалась" in out
    assert "жодних ордерів, ліміти не змінюються" in out


def test_without_a_note_the_note_is_cleared_as_journal_close_does(journal_db):
    tid = add(journal_db, symbol="ETH", side="short", size=1.0, entry=2400.0)
    con = sqlite3.connect(journal_db)
    con.execute("UPDATE journal SET note = 'imported' WHERE id = ?", (tid,))
    con.commit()
    con.close()
    out = jx.handle(f"/cancel {tid}", journal_db, NOW)
    assert row(journal_db, tid)["status"] == "cancelled" and row(journal_db, tid)["note"] is None
    assert "Нотатка" not in out and "ETH · шорт" in out


@pytest.mark.parametrize("tags", [["demo-paper", "bybit-demo", "setup:S14", "auto"], ["auto"], ["demo-paper"], ["bybit-demo"]])
def test_a_demo_trader_row_is_refused(journal_db, tags):
    tid = add(journal_db, symbol="BTC/USDT:USDT", side="long", size=0.01, entry=84000.0, tags=tags)
    before = row(journal_db, tid)
    out = jx.handle(f"/cancel {tid}", journal_db, NOW)
    assert out == f"❌ Угоду не скасовано: угода #{tid} — це позиція демо-бота, нею керує бот; журнал не змінено."
    assert row(journal_db, tid) == before


def test_unknown_id_closed_and_cancelled_change_nothing(journal_db):
    assert jx.handle("/cancel 99", journal_db, NOW) == "❌ Угоду не скасовано: угоди #99 у журналі немає."
    closed = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    con = sqlite3.connect(journal_db)
    con.execute("UPDATE journal SET status = 'closed', exit = 86000, pnl = 200, outcome = 'win' WHERE id = ?", (closed,))
    con.commit()
    con.close()
    before = row(journal_db, closed)
    assert jx.handle(f"/cancel {closed}", journal_db, NOW) == f"❌ Угоду не скасовано: угода #{closed} уже закрита; журнал не змінено."
    assert row(journal_db, closed) == before
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    jx.handle(f"/cancel {tid} note first", journal_db, NOW)
    before = row(journal_db, tid)
    out = jx.handle(f"/cancel {tid} note second", journal_db, NOW + 60)
    assert out == f"❌ Угоду не скасовано: угода #{tid} скасована; журнал не змінено."
    assert row(journal_db, tid) == before


def test_a_row_closed_meanwhile_is_not_overwritten(journal_db, monkeypatch):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    con = sqlite3.connect(journal_db)
    con.execute("UPDATE journal SET status = 'closed', exit = 83000, pnl = -100 WHERE id = ?", (tid,))
    con.commit()
    con.close()
    before = row(journal_db, tid)
    real_get = Store.journal_get
    calls = []

    async def stale_first(self, trade_id):  # the first read predates the other process's close
        got = await real_get(self, trade_id)
        calls.append(trade_id)
        return {**got, "status": "open"} if len(calls) == 1 else got

    monkeypatch.setattr(jx.Store, "journal_get", stale_first)
    out = jx.handle(f"/cancel {tid}", journal_db, NOW)
    assert out == f"❌ Угоду не скасовано: угода #{tid} уже закрита; журнал не змінено."
    assert row(journal_db, tid) == before and len(calls) == 2


def test_a_failure_after_the_write_does_not_claim_the_trade_is_open(journal_db, monkeypatch):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    real_close = Store.aclose

    async def close_then_fail(self):
        await real_close(self)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(jx.Store, "aclose", close_then_fail)
    out = jx.handle(f"/cancel {tid}", journal_db, NOW)
    assert row(journal_db, tid)["status"] == "cancelled"
    assert out.startswith(f"✅ Скасовано в журналі: угода #{tid}. Після запису збій бази (OperationalError)")
    assert "Спробуйте ще раз" not in out


def test_a_failed_reply_still_says_the_trade_is_cancelled(journal_db, monkeypatch):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)

    def broken(*a, **k):
        raise KeyError("symbol")

    monkeypatch.setattr(jx, "build_reply", broken)
    out = jx.handle(f"/cancel {tid}", journal_db, NOW)
    assert out == f"✅ Скасовано в журналі: угода #{tid}. Відповідь не вдалося зібрати (KeyError)."
    assert row(journal_db, tid)["status"] == "cancelled"


def test_a_bad_command_writes_nothing_and_shows_the_format(journal_db):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    out = jx.handle(f"/cancel {tid} 86000", journal_db, NOW)  # a mistyped /close
    assert out.startswith("❌ Угоду не скасовано: після id дозволено лише note") and "Формат:" in out
    assert jx.handle("/cancel x", journal_db, NOW).startswith("❌ Угоду не скасовано: id — ціле додатне число")
    assert row(journal_db, tid)["status"] == "open"
    for code in ("empty", "not_cancel_command", "too_long", "missing_id", "bad_id", "unknown_word", "note_too_long"):
        assert code in jx.ERRORS_UA


def test_untrusted_note_is_escaped_in_the_reply_and_stored_as_text(journal_db):
    tid = add(journal_db, symbol="BTC", side="short", size=0.1, entry=84000.0)
    out = jx.handle(f"/cancel {tid} note <b>x</b> & raise limit", journal_db, NOW)
    assert "Нотатка: &lt;b&gt;x&lt;/b&gt; &amp; raise limit" in out and "<b>x</b>" not in out
    assert row(journal_db, tid)["note"] == "<b>x</b> & raise limit"
    bare = re.compile(r"&(?!amp;|lt;|gt;|quot;)")
    for text in (out, jx.handle("/cancel x", journal_db, NOW)):
        assert not bare.search(text), text


def test_no_database_file_is_refused_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    out = jx.handle("/cancel 1", path, NOW)
    assert out.startswith("❌ Угоду не скасовано: файла журналу") and not path.exists()


def test_a_store_failure_gives_a_reason_and_never_raises(journal_db, monkeypatch):
    async def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jx, "cancel_trade", boom)
    assert jx.handle("/cancel 1", journal_db, NOW).startswith("❌ Угоду не скасовано: збій бази (OperationalError)")


def test_main_prints_the_reply(journal_db, capsys):
    tid = add(journal_db, symbol="ETH", side="long", size=1.0, entry=2400.0)
    assert jx.main([f"--text=/cancel {tid}", "--db", str(journal_db)]) == 0
    assert f"угода #{tid}" in capsys.readouterr().out
    assert row(journal_db, tid)["status"] == "cancelled"
