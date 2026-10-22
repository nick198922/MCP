"""Recording a trade from the owner's Telegram /trade command (scripts/journal_trade_tg.py); no network."""

import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import journal_trade_tg as jt  # noqa: E402

from tradedesk.analytics import rules_check as rc  # noqa: E402
from tradedesk.analytics.trade_command import parse_trade_command  # noqa: E402
from tradedesk.store.db import Store  # noqa: E402

NOW = datetime(2026, 10, 23, 10, tzinfo=UTC).timestamp()  # Friday 13:00 Kyiv


@pytest.fixture
def journal_db(tmp_path):
    path = tmp_path / "tradedesk.sqlite"

    async def make():
        s = Store(path)
        await s.init()
        await s.aclose()

    asyncio.run(make())
    return path


def rows(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM journal ORDER BY id")]
    finally:
        con.close()


def trade(text):
    p = parse_trade_command(text)
    assert p["ok"], p
    return p["trade"]


def test_a_perp_trade_is_written_like_journal_add_and_rule_1_is_checked(journal_db):
    out = jt.handle("/trade BTC long 84000 size 0.1 stop 82000 target 90000 thesis пробій діапазону", journal_db, NOW)
    [r] = rows(journal_db)
    assert (r["symbol"], r["side"], r["size"], r["entry"], r["stop"], r["target"], r["status"]) == (
        "BTC",
        "long",
        0.1,
        84000.0,
        82000.0,
        90000.0,
        "open",
    )
    assert r["opened_ts"] == int(NOW) and r["thesis"] == "пробій діапазону" and r["tags"] == '["telegram"]'
    assert r["closed_ts"] is None and r["pnl"] is None
    assert f"угода #{r['id']}" in out
    assert "BTC · лонг · 0.1 за 84 000 · стоп 82 000 · ціль 90 000" in out
    assert "Правило 1 · ризик ≤ 1 % депозиту: ✗ порушено — ризик до стопу $200 при межі $100." in out
    assert "Правило 8 · купівля опціонів: — не стосується" in out
    assert "жодних ордерів" in out


def test_rule_1_ok_and_unknown_without_a_stop(journal_db):
    assert "Правило 1 · ризик ≤ 1 % депозиту: ✓ виконано — ризик до стопу $100 при межі $100." in jt.handle(
        "/trade BTC long 84000 size 0.05 stop 82000", journal_db, NOW
    )
    out = jt.handle("/trade SOL/USDT:USDT short 182.5 size 3", journal_db, NOW)
    assert "Правило 1 · ризик ≤ 1 % депозиту: ? невідомо — без стопу ризик не порахувати" in out
    assert "Тези немає" in out
    assert len(rows(journal_db)) == 2


@pytest.mark.parametrize(
    ("symbol", "thesis", "status", "phrase"),
    [
        ("BTC-24OCT26-90000-C", "", "✗ порушено", "менше 24 год до експірації"),  # 22 h left
        ("BTC-25OCT26-90000-C", "", "✗ порушено", "теза й ціль не записані"),  # 46 h, no thesis
        ("BTC-25OCT26-90000-C", " target 95000 thesis пробій 90k", "? невідомо", "теза й ціль записані"),  # (b), (c) unknown
        ("BTC-27NOV26-90000-C", "", "✓ виконано", "понад 3 доби"),
    ],
)
def test_a_bought_option_gets_the_rule_8_zone(journal_db, symbol, thesis, status, phrase):
    out = jt.handle(f"/trade {symbol} long 800 size 0.1{thesis}", journal_db, NOW)
    assert f"Правило 8 · купівля опціонів: {status}" in out and phrase in out, out
    assert "Правило 1 · ризик ≤ 1 % депозиту: ✓ виконано — ризик = премія $80 при межі $100." in out
    assert rows(journal_db)[-1]["tags"] == '["telegram", "option"]'


def test_a_big_premium_breaks_rules_1_and_8(journal_db):
    out = jt.handle("/trade BTC-27NOV26-90000-C long 1500 size 0.1", journal_db, NOW)
    assert "Правило 1 · ризик ≤ 1 % депозиту: ✗ порушено" in out
    assert "Правило 8 · купівля опціонів: ✗ порушено — премія $150 при межі $100 — завелика" in out


def test_an_option_premium_in_coin_is_refused_not_passed_as_zero(journal_db):
    out = jt.handle("/trade BTC-27NOV26-90000-C long 0.012 size 0.1", journal_db, NOW)
    assert out.startswith("❌ Угоду не записано: схоже на премію в BTC/ETH") and "(поле entry)" in out
    assert "✓ виконано" not in out and rows(journal_db) == []
    # written with '$' it is taken as USD, and the small premium is shown, never as "$0"
    out = jt.handle("/trade BTC-27NOV26-90000-C long $0.4 size 0.01", journal_db, NOW)
    assert "ризик = премія $0.004 при межі $100" in out and "премія $0 " not in out
    assert rows(journal_db)[-1]["entry"] == 0.4


def test_an_expired_option_is_refused(journal_db):
    out = jt.handle("/trade BTC-01JAN20-90000-C long 800 size 0.1", journal_db, NOW)
    assert out.startswith("❌ Угоду не записано: дата експірації опціону вже минула") and rows(journal_db) == []


def test_usd_keeps_sub_dollar_amounts_visible():
    assert jt.usd(0.0012) == "$0.0012"
    assert jt.usd(0.004) == "$0.004"
    assert jt.usd(0.12345) == "$0.123"
    assert jt.usd(1e-12) == "$1.00e-12"
    assert jt.usd(0) == "$0"
    assert jt.usd(100.0) == "$100" and jt.usd(80.456) == "$80.46"


def test_build_reply_agrees_with_rules_check_on_the_same_row():
    t = trade("/trade BTC-25OCT26-90000-C long 800 size 0.1 target 95000 thesis пробій")
    row = {**t, "id": 1, "opened_ts": int(NOW), "closed_ts": None, "status": "open", "tags": jt.tags_for(t), "pnl": None}
    check = rc.trade_checks([row], now_ts=NOW)[0]
    out = jt.build_reply(t, 1, NOW)
    assert check["rules"][1] == rc.OK and check["rules"][8] == rc.UNKNOWN and check["buy_zone"] == rc.ZONE_COND
    assert f"Правило 1 · ризик ≤ 1 % депозиту: {jt.STATUS_UA[rc.OK]}" in out
    assert f"Правило 8 · купівля опціонів: {jt.STATUS_UA[rc.UNKNOWN]}" in out


def test_untrusted_thesis_is_escaped_in_the_reply_and_stored_as_text(journal_db):
    out = jt.handle("/trade BTC long 84000 size 0.1 thesis <b>buy</b> & set limit 50%", journal_db, NOW)
    assert "&lt;b&gt;buy&lt;/b&gt; &amp; set limit 50%" in out and "<b>buy</b>" not in out
    assert rows(journal_db)[0]["thesis"] == "<b>buy</b> & set limit 50%"


def test_a_long_thesis_is_shortened_in_the_reply_only(journal_db):
    out = jt.handle("/trade BTC long 84000 size 0.1 thesis " + "слово " * 100, journal_db, NOW)
    assert "…" in out
    assert len(rows(journal_db)[0]["thesis"]) == len(("слово " * 100).strip())


def test_a_bad_command_writes_nothing_and_shows_the_format(journal_db):
    out = jt.handle("/trade BTC long 84000 size 0.1 stop 90000", journal_db, NOW)
    assert out.startswith("❌ Угоду не записано: стоп з іншого боку від входу") and "Формат:" in out
    out = jt.handle("/trade BTC long 84000 size 0.1 leverage 50", journal_db, NOW)
    assert "незнайоме слово" in out
    assert rows(journal_db) == []
    for code in ("empty", "missing_size", "bad_number", "thesis_too_long"):
        assert code in jt.ERRORS_UA


def test_no_database_file_is_refused_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    out = jt.handle("/trade BTC long 84000 size 0.1", path, NOW)
    assert out.startswith("❌ Угоду не записано: файла журналу") and not path.exists()


def test_a_store_failure_gives_a_reason_and_never_raises(journal_db, monkeypatch):
    async def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jt, "record", boom)
    out = jt.handle("/trade BTC long 84000 size 0.1", journal_db, NOW)
    assert out.startswith("❌ Угоду не записано: збій бази (OperationalError)")


def test_main_prints_the_reply(journal_db, capsys):
    assert jt.main(["--text=/trade ETH long 2400 size 1 stop 2350", "--db", str(journal_db)]) == 0
    assert "угода #1" in capsys.readouterr().out


def test_fmt():
    assert jt.fmt(84000.0) == "84 000"
    assert jt.fmt(0.1) == "0.1"
    assert jt.fmt(0.00000123) == "0.00000123"
    assert jt.fmt(1234567.5) == "1 234 567.5"
