"""The pre-trade rules check from the owner's Telegram /check command (scripts/rules_pretrade_tg.py); temp DB, no network."""

import asyncio
import hashlib
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import journal_trade_tg as jt  # noqa: E402
import rules_pretrade_tg as rp  # noqa: E402

from tradedesk.store.db import Store  # noqa: E402

NOW = datetime(2026, 10, 25, 10, tzinfo=UTC).timestamp()  # Sunday 13:00 Kyiv
H = 3600.0
RULE_RE = re.compile(r"^Правило (\d) · [^:]*: (✓ виконано|✗ порушено|\? невідомо|— не стосується)", re.M)


def statuses(reply):
    return dict(RULE_RE.findall(reply))


def _row(i, symbol, side, opened, closed=None, pnl=None, tags='["hyperliquid", "import", "perp"]', **kw):
    return {
        "id": i,
        "symbol": symbol,
        "side": side,
        "opened_ts": int(opened),
        "closed_ts": None if closed is None else int(closed),
        "status": "open" if closed is None else "closed",
        "pnl": pnl,
        "size": kw.get("size", 1.0),
        "entry": kw.get("entry", 1.0),
        "stop": kw.get("stop"),
        "target": kw.get("target"),
        "thesis": None,
        "tags": tags,
    }


@pytest.fixture
def journal_db(tmp_path):
    path = tmp_path / "tradedesk.sqlite"

    async def make():
        s = Store(path)
        await s.init()
        await s.aclose()

    asyncio.run(make())
    return path


def test_an_option_purchase_in_the_one_to_three_day_zone_is_never_ok_without_the_live_checks(journal_db):
    out = rp.handle("/check BTC call premium 80 expiry 36h target 95000 thesis пробій діапазону", journal_db, NOW)
    lines = out.split("\n")
    assert len(lines) <= 8 and lines[-1].startswith("<i>Статистика дотримання правил, не порада")
    assert "жодних ордерів" in lines[-1] and "нічого не записано" in lines[-1]
    assert statuses(out) == {"1": "✓ виконано", "2": "✓ виконано", "4": "✓ виконано", "5": "— не стосується", "8": "? невідомо"}
    assert lines[0] == "<b>Перевірка перед угодою: ? невідомо: 8</b>"  # unknown is not ok
    assert "BTC кол · купівля опціону · премія $80 · до експірації 1.5 дн. (36 год) · ціль 95 000 · теза є" in out
    assert "разом з цією $80 при межі $300 (відкритих у журналі 0)" in out


@pytest.mark.parametrize(
    ("text", "rule8", "phrase"),
    [
        ("/check BTC put premium 80 expiry 12h", "✗ порушено", "менше 24 год до експірації"),
        ("/check BTC call premium 80 expiry 2d", "✗ порушено", "немає тези й цілі — умова (а) не виконана"),
        ("/check BTC call premium 80 expiry 24h thesis x", "✗ порушено", "немає цілі — умова (а) не виконана"),
        ("/check BTC call premium 80 expiry 2d target 95000", "✗ порушено", "немає тези — умова (а) не виконана"),
        ("/check BTC call premium 80 expiry 2d target 95000 thesis x", "? невідомо", "теза й ціль є"),
        ("/check BTC call premium 150 expiry 5d", "✗ порушено", "завелика"),
        ("/check BTC call premium 80 expiry 5d", "✓ виконано", "понад 3 доби — лише ліміт премії"),
        ("/check BTC-27OCT26-90000-C premium 80 target 95000 thesis так", "? невідомо", "1–3 доби"),  # 46 h from the symbol
    ],
)
def test_rule_8_zones(journal_db, text, rule8, phrase):
    out = rp.handle(text, journal_db, NOW)
    assert statuses(out)["8"] == rule8 and phrase in out


def test_a_linear_trade_rules_1_and_5():
    out = rp.build_reply(rp.parse_check_command("/check SOL long 180 stop 170 target 200 risk 90", NOW)["check"], NOW, [])
    assert statuses(out) == {"1": "✓ виконано", "2": "✓ виконано", "4": "✓ виконано", "5": "✓ виконано", "8": "— не стосується"}
    assert out.startswith("<b>Перевірка перед угодою: ✓ виконано: 1, 2, 4, 5</b>")
    out = rp.build_reply(rp.parse_check_command("/check BTC long 84000", NOW)["check"], NOW, [])
    assert statuses(out)["1"] == statuses(out)["5"] == "? невідомо" and "додайте stop" in out
    out = rp.build_reply(rp.parse_check_command("/check BTC long 84000 stop 82000 target 85000 size 0.1", NOW)["check"], NOW, [])
    assert statuses(out)["1"] == statuses(out)["5"] == "✗ порушено" and "✗ порушено: 1, 5" in out


def test_rules_2_and_4_come_from_the_journal():
    alts = [_row(i, s, "long", NOW - 5 * H, stop=0.5) for i, s in enumerate(("SOL", "AVAX", "LINK"), 1)]
    check = rp.parse_check_command("/check DOGE long 0.2 stop 0.19 risk 50", NOW)["check"]
    out = rp.build_reply(check, NOW, alts)
    assert statuses(out)["2"] == "✗ порушено" and "лонгів в альтах з цією 4 — новий альт понад межу" in out
    assert "разом з цією $51.5 при межі $300 (відкритих у журналі 3)" in out
    losses = [_row(10 + i, "BTC", "long", NOW - (5 - i) * H, NOW - (4 - i) * H, -20.0) for i in range(2)]
    out = rp.build_reply(rp.parse_check_command("/check BTC call premium 50 expiry 5d", NOW)["check"], NOW, losses)
    assert statuses(out)["4"] == "✗ порушено" and "сьогодні спрацював о 07:00 UTC (2 збиткові поспіль)" in out
    # paper and airdrop rows are not the owner's trades
    paper = [{**r, "tags": '["carry-paper"]'} for r in alts]
    assert statuses(rp.build_reply(check, NOW, paper))["2"] == "✓ виконано"


def test_an_unreadable_journal_makes_rules_2_and_4_unknown(tmp_path):
    out = rp.handle("/check SOL long 180 stop 170 target 200 risk 90", tmp_path / "missing.sqlite", NOW)
    assert statuses(out)["2"] == statuses(out)["4"] == "? невідомо" and out.count("журнал не прочитано") == 2
    assert statuses(out)["1"] == "✓ виконано" and "? невідомо: 2, 4" in out.split("\n")[0]
    assert not (tmp_path / "missing.sqlite").exists()  # never created


def test_the_check_matches_the_trade_answer_on_the_same_line(journal_db, tmp_path):
    jt.handle("/trade AVAX long 30 size 2 stop 28", journal_db, NOW - H)  # one open trade in both journals
    for i, line in enumerate(
        (
            "BTC long 84000 size 0.1 stop 82000 target 90000 thesis x",
            "SOL/USDT:USDT short 182.5 size 3",
            "BTC-27OCT26-90000-C long 1200 size 0.01 target 95000 thesis чому",
            "BTC-26OCT26-90000-P long 150 size 1",
        )
    ):
        other = tmp_path / f"trade{i}.sqlite"  # /trade writes its row: a fresh copy per line
        other.write_bytes(journal_db.read_bytes())
        assert statuses(rp.handle("/check " + line, journal_db, NOW)) == statuses(jt.handle("/trade " + line, other, NOW)), line


def test_the_check_matches_the_trade_answer_at_a_fractional_second_on_the_24_hour_edge(journal_db):
    now = datetime(2026, 10, 25, 8, tzinfo=UTC).timestamp() + 0.6  # BTC-26OCT26 expires 24 h after the whole second
    line = "BTC-26OCT26-90000-C long 150 size 0.5 target 95000 thesis x"
    out = rp.handle("/check " + line, journal_db, now)
    assert statuses(out) == statuses(jt.handle("/trade " + line, journal_db, now))
    assert statuses(out)["8"] == "? невідомо" and "1–3 доби" in out  # the 1-3 day zone, as /trade, not the ban


def test_a_premium_in_coin_is_refused_and_never_shown_as_kept(journal_db):
    for text in ("/check BTC-31OCT26-90000-C premium 0.012", "/check BTC call premium 0.015 expiry 5d"):
        out = rp.handle(text, journal_db, NOW)
        assert out.startswith("❌ Перевірку не виконано: схоже на премію в BTC/ETH") and "✓" not in out
    out = rp.handle("/check BTC-31OCT26-90000-C put premium 80", journal_db, NOW)
    assert out.startswith("❌ Перевірку не виконано: call / put суперечить символу опціону")


def test_nothing_is_written_and_user_text_is_escaped(journal_db):
    jt.handle("/trade BTC long 84000 size 0.1 stop 82000", journal_db, NOW)
    before = hashlib.sha256(journal_db.read_bytes()).hexdigest()
    out = rp.handle("/check xyz:<b>X long 1", journal_db, NOW)
    assert out.startswith("❌ Перевірку не виконано: символ") and "<b>X" not in out
    out = rp.handle("/check BTC call premium 80 expiry 4d thesis <script>‮", journal_db, NOW)
    assert "<script>" not in out and "‮" not in out
    assert hashlib.sha256(journal_db.read_bytes()).hexdigest() == before


def test_a_bad_command_gives_the_format_and_a_failure_never_raises(journal_db, monkeypatch):
    for text in ("/check", "/check BTC call premium 80", "/check BTC long 84000 risk 90", "/check BTC-27OCT26-90000-C short premium 1"):
        out = rp.handle(text, journal_db, NOW)
        assert out.startswith("❌ Перевірку не виконано:") and "Формат:" in out
    for code in rp.ERRORS_UA:
        assert rp.error_reply({"error": code}).startswith("❌ Перевірку не виконано")
    monkeypatch.setattr(rp.rc, "trade_checks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = rp.handle("/check BTC call premium 80 expiry 4d", journal_db, NOW)
    assert out == "❌ Перевірку не вдалося зібрати (RuntimeError). Нічого не записано."


def test_every_parser_error_code_has_ukrainian_words():
    import tradedesk.analytics.check_command as cc

    codes = set(re.findall(r"_err\(\s*\"(\w+)\"", Path(cc.__file__).read_text(encoding="utf-8")))
    assert codes and codes <= set(rp.ERRORS_UA)
