"""Closing a journal trade from the owner's Telegram /close command (scripts/journal_close_tg.py); temp DB, no network."""

import asyncio
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import journal_close_tg as jc  # noqa: E402

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


def test_a_long_is_closed_like_journal_close_with_r(journal_db):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0, stop=82000.0, tags=["telegram"])
    out = jc.handle(f"/close {tid} 86000 note вийшов до CPI", journal_db, NOW)
    r = row(journal_db, tid)
    assert (r["status"], r["exit"], r["outcome"], r["closed_ts"]) == ("closed", 86000.0, "win", int(NOW))
    assert r["pnl"] == pytest.approx(200.0) and r["r_multiple"] == pytest.approx(1.0)
    assert r["note"] == "вийшов до CPI"
    assert f"угода #{tid}" in out and "BTC · лонг · 0.1 · вхід 84 000 → вихід 86 000" in out
    assert "P&amp;L: +$200 (у плюс) — без комісій (не вказано fees)." in out
    assert "R: +1.00 (стоп 82 000; за ціною, без комісії)." in out
    assert "жодних ордерів, ліміти не змінюються" in out


def test_fees_are_subtracted_and_noted(journal_db):
    tid = add(journal_db, symbol="SOL/USDT:USDT", side="short", size=3.0, entry=182.5, stop=190.0)
    out = jc.handle(f"/close #{tid} @ 180.5 комісія 1,5", journal_db, NOW)
    r = row(journal_db, tid)
    assert r["pnl"] == pytest.approx(4.5) and r["outcome"] == "win"  # gross 6.0 minus 1.5
    assert r["r_multiple"] == pytest.approx(2.0 / 7.5)  # price R, fees not in it
    assert r["note"] == "fees $1.5 (pnl net of fees)"
    assert "P&amp;L: +$4.5 (у плюс) — за вирахуванням комісії $1.5; без неї +$6." in out
    assert "R: +0.27" in out


def test_fees_can_turn_a_small_win_into_a_loss(journal_db):
    tid = add(journal_db, symbol="ETH", side="long", size=1.0, entry=2400.0)
    out = jc.handle(f"/close {tid} 2401 fees 3 note scratch", journal_db, NOW)
    r = row(journal_db, tid)
    assert r["pnl"] == pytest.approx(-2.0) and r["outcome"] == "loss" and r["r_multiple"] is None
    assert r["note"] == "fees $3 (pnl net of fees); scratch"
    assert "P&amp;L: -$2 (у мінус)" in out and "R: невідомо — стоп не записано." in out


def test_a_bought_option_gets_no_r_but_the_result_on_premium(journal_db):
    tid = add(journal_db, symbol="BTC-27NOV26-90000-C", side="long", size=0.1, entry=800.0, stop=82000.0, tags=["option"])
    out = jc.handle(f"/close {tid} 1200", journal_db, NOW)
    r = row(journal_db, tid)
    assert r["pnl"] == pytest.approx(40.0) and r["r_multiple"] is None
    assert "R: не рахую — для опціону стоп стосується ціни базового активу, не премії; результат до сплаченої премії +50%." in out


def test_an_option_exit_in_coin_is_refused(journal_db):
    tid = add(journal_db, symbol="BTC-27NOV26-90000-C", side="long", size=0.1, entry=800.0)
    out = jc.handle(f"/close {tid} 0.012", journal_db, NOW)
    assert out.startswith("❌ Угоду не закрито: схоже на премію в BTC/ETH") and "(поле exit)" in out
    assert row(journal_db, tid)["status"] == "open"
    jc.handle(f"/close {tid} $0.4", journal_db, NOW)  # with '$' it is USD per coin
    assert row(journal_db, tid)["exit"] == 0.4


def test_an_expired_option_closes_at_zero_but_a_linear_trade_does_not(journal_db):
    tid = add(journal_db, symbol="BTC-27NOV26-90000-C", side="long", size=0.1, entry=800.0, tags=["option"])
    out = jc.handle(f"/close {tid} 0", journal_db, NOW)
    r = row(journal_db, tid)
    assert (r["status"], r["exit"], r["outcome"]) == ("closed", 0.0, "loss") and r["pnl"] == pytest.approx(-80.0)
    assert "вихід 0" in out and "P&amp;L: -$80 (у мінус)" in out and "результат до сплаченої премії -100%" in out
    lin = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    for text in (f"/close {lin} 0", f"/close {lin} $0"):
        out = jc.handle(text, journal_db, NOW)
        assert out.startswith("❌ Угоду не закрито: число має бути додатним") and "(поле exit)" in out, text
    assert row(journal_db, lin)["status"] == "open"
    assert "/close 12 0" in jc.USAGE


def test_a_sold_option_gets_no_r_and_no_premium_line(journal_db):
    tid = add(journal_db, symbol="ETH-27NOV26-2000-P", side="short", size=2.0, entry=50.0, stop=2100.0)
    out = jc.handle(f"/close {tid} 20", journal_db, NOW)
    r = row(journal_db, tid)
    assert r["pnl"] == pytest.approx(60.0) and r["outcome"] == "win" and r["r_multiple"] is None
    assert "R: не рахую — для опціону стоп стосується ціни базового активу, не премії." in out
    assert "до сплаченої премії" not in out  # that ratio is only for a bought option


def test_a_stop_on_the_wrong_side_gives_no_r(journal_db):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0, stop=86000.0)
    out = jc.handle(f"/close {tid} 85000", journal_db, NOW)
    assert row(journal_db, tid)["r_multiple"] is None
    assert "R: невідомо — стоп записано не з того боку від входу." in out


def test_a_row_with_another_side_is_refused(journal_db):
    tid = add(journal_db, symbol="BTC", side="buy", size=0.1, entry=84000.0)
    out = jc.handle(f"/close {tid} 86000", journal_db, NOW)
    assert out.startswith("❌ Угоду не закрито: у цієї угоди напрям не long і не short")
    assert row(journal_db, tid)["status"] == "open"


@pytest.mark.parametrize("tags", [["demo-paper", "bybit-demo", "setup:S14", "tf:4h", "auto"], ["auto"], ["demo-paper"], ["bybit-demo"]])
def test_a_demo_trader_row_is_refused_and_left_to_the_bot(journal_db, tags):
    tid = add(journal_db, symbol="BTC/USDT:USDT", side="long", size=0.01, entry=84000.0, stop=82000.0, tags=tags)
    before = row(journal_db, tid)
    out = jc.handle(f"/close {tid} 86000", journal_db, NOW)
    assert out == f"❌ Угоду не закрито: угода #{tid} — це позиція демо-бота, вона закривається ботом; журнал не змінено."
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

    monkeypatch.setattr(jc.Store, "journal_get", stale_first)
    out = jc.handle(f"/close {tid} 86000", journal_db, NOW)
    assert out == f"❌ Угоду не закрито: угода #{tid} уже закрита; журнал не змінено."
    assert row(journal_db, tid) == before and len(calls) == 2


def test_a_failure_after_the_write_does_not_claim_the_trade_is_open(journal_db, monkeypatch):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    real_close = Store.aclose

    async def close_then_fail(self):
        await real_close(self)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(jc.Store, "aclose", close_then_fail)
    out = jc.handle(f"/close {tid} 86000", journal_db, NOW)
    assert row(journal_db, tid)["status"] == "closed"
    assert out.startswith(f"✅ Закрито в журналі: угода #{tid}. Після запису збій бази (OperationalError)")
    assert "Спробуйте ще раз" not in out


def test_a_failed_reply_still_says_the_trade_is_closed(journal_db, monkeypatch):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)

    def broken(*a, **k):
        raise KeyError("pnl")

    monkeypatch.setattr(jc, "build_reply", broken)
    out = jc.handle(f"/close {tid} 86000", journal_db, NOW)
    assert out == f"✅ Закрито в журналі: угода #{tid}. Відповідь не вдалося зібрати (KeyError)."
    assert row(journal_db, tid)["status"] == "closed"


def test_unknown_id_and_already_closed_change_nothing(journal_db):
    assert jc.handle("/close 99 86000", journal_db, NOW) == "❌ Угоду не закрито: угоди #99 у журналі немає."
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    jc.handle(f"/close {tid} 86000", journal_db, NOW)
    before = row(journal_db, tid)
    out = jc.handle(f"/close {tid} 70000", journal_db, NOW + 60)
    assert out == f"❌ Угоду не закрито: угода #{tid} уже закрита; журнал не змінено."
    assert row(journal_db, tid) == before


def test_a_cancelled_trade_is_not_reopened(journal_db):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    con = sqlite3.connect(journal_db)
    con.execute("UPDATE journal SET status = 'cancelled' WHERE id = ?", (tid,))
    con.commit()
    con.close()
    assert "скасована" in jc.handle(f"/close {tid} 86000", journal_db, NOW)
    assert row(journal_db, tid)["exit"] is None


def test_a_bad_price_writes_nothing_and_shows_the_format(journal_db):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0)
    for text in (f"/close {tid} 86,000", f"/close {tid} -1", f"/close {tid} nan", f"/close {tid} 0"):
        out = jc.handle(text, journal_db, NOW)
        assert out.startswith("❌ Угоду не закрито: число має бути додатним") and "Формат:" in out, text
    assert "незнайоме слово" in jc.handle(f"/close {tid} 86000 cancel", journal_db, NOW)
    assert row(journal_db, tid)["status"] == "open"
    for code in ("empty", "missing_id", "bad_id", "missing_exit", "note_too_long"):
        assert code in jc.ERRORS_UA


def test_untrusted_note_is_escaped_in_the_reply_and_stored_as_text(journal_db):
    tid = add(journal_db, symbol="BTC", side="short", size=0.1, entry=84000.0)
    out = jc.handle(f"/close {tid} 84000 note <b>x</b> & raise limit", journal_db, NOW)
    assert "Нотатка: &lt;b&gt;x&lt;/b&gt; &amp; raise limit" in out and "<b>x</b>" not in out
    assert "P&amp;L: $0 (в нуль)" in out
    assert row(journal_db, tid)["note"] == "<b>x</b> & raise limit"


def test_replies_have_no_bare_ampersand_for_telegram_html(journal_db):
    tid = add(journal_db, symbol="BTC", side="long", size=0.1, entry=84000.0, stop=82000.0)
    bare = re.compile(r"&(?!amp;|lt;|gt;|quot;)")
    for out in (jc.handle(f"/close {tid} 86000 fees 2 note a & b", journal_db, NOW), jc.handle("/close x", journal_db, NOW)):
        assert not bare.search(out), out


def test_no_database_file_is_refused_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    out = jc.handle("/close 1 86000", path, NOW)
    assert out.startswith("❌ Угоду не закрито: файла журналу") and not path.exists()


def test_a_store_failure_gives_a_reason_and_never_raises(journal_db, monkeypatch):
    async def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jc, "close_trade", boom)
    assert jc.handle("/close 1 86000", journal_db, NOW).startswith("❌ Угоду не закрито: збій бази (OperationalError)")


def test_main_prints_the_reply(journal_db, capsys):
    tid = add(journal_db, symbol="ETH", side="long", size=1.0, entry=2400.0, stop=2350.0)
    assert jc.main([f"--text=/close {tid} 2450", "--db", str(journal_db)]) == 0
    assert f"угода #{tid}" in capsys.readouterr().out
