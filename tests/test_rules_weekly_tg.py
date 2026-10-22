"""Weekly rule-violation summary for Telegram (scripts/rules_weekly_tg.py); no network."""

import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard as db  # noqa: E402
import rules_weekly_tg as rw  # noqa: E402
import tg_send  # noqa: E402

import test_rules_check as trc  # noqa: E402
from tradedesk.analytics import rules_check as rc  # noqa: E402
from tradedesk.store.schema import MIGRATIONS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAY, H = 86400.0, 3600.0
SEND_AT = datetime(2025, 10, 20, 6, tzinfo=UTC).timestamp()  # Monday 09:00 Kyiv: ISO week 2025-W43 has just begun
COLS = ("id", "opened_ts", "closed_ts", "symbol", "side", "size", "entry", "stop", "target", "thesis", "tags", "status", "pnl")


@pytest.fixture(autouse=True)
def _no_dashboard_url(tmp_path, monkeypatch):
    """The server's real URL file (step 23), key file or a set $TRADEDESK_PANEL_URL must not change these tests."""
    monkeypatch.setattr(rw.ab, "DASHBOARD_URL_PATH", tmp_path / "no_dashboard_url")
    monkeypatch.setattr(db, "KEY_FILE", tmp_path / "no_dashboard.key")
    monkeypatch.delenv(rw.ab.PANEL_URL_ENV, raising=False)


def _shift(rows: list[dict], days: float, id_base: int = 0) -> list[dict]:
    return [
        {
            **r,
            "id": id_base + r["id"],
            "opened_ts": int(r["opened_ts"] + days * DAY),
            "closed_ts": None if r["closed_ts"] is None else int(r["closed_ts"] + days * DAY),
        }
        for r in rows
    ]


def _journal() -> list[dict]:
    """Rows shaped as the imports write them (tests/test_rules_check.py): the day-stop rows on Fri 17.10 and Sat 18.10
    and the alt longs on Sun 19.10 (ISO week 2025-W42), a 0DTE option bought on Mon 20.10 (W43), plus a paper and an
    airdrop row that must not count."""
    rows = _shift(trc._day_stop_rows(), 7) + _shift(trc._alt_rows(), 9, 100)
    rows.append(
        trc._t(300, "BTC-20OCT25-110000-C", "long", SEND_AT - 5 * H, None, None, trc.BYBIT_OPT, size=0.01, entry=100.0, status="open")
    )
    rows.append(trc._t(901, "SOL", "long", trc.D + 9 * DAY + 7 * H, trc.D + 9 * DAY + 8 * H, -999.0, '["carry-paper"]'))
    rows.append(trc._t(902, "ARB", "long", trc.D + 9 * DAY + 7 * H, trc.D + 9 * DAY + 8 * H, 5.0, '["airdrop"]'))
    return rows


def _store(path: Path, version: int, rows: list[dict] = ()) -> Path:
    """A SQLite file migrated to `version` with the real schema, holding `rows` in journal."""
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= version:
            for sql in MIGRATIONS[v]:
                con.execute(sql)
    for r in rows:
        con.execute(f"INSERT INTO journal ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})", [r.get(c) for c in COLS])
    con.commit()
    con.close()
    return path


def test_summary_on_a_real_shaped_journal(tmp_path):
    path = _store(tmp_path / "t.sqlite", 23, _journal())
    before = path.read_bytes()
    got, err = rw.load_journal(path)
    assert err is None and len(got) == 19
    assert path.read_bytes() == before  # read-only
    text = rw.build_summary(got, SEND_AT)
    lines = text.splitlines()
    assert len(lines) <= 8 and len(text) < 2000, text
    assert lines[0].startswith("<b>Правила власника за тиждень · 20.10 09:00") and lines[-1] == f"<i>{rw._h(rw.CAVEAT)}</i>"
    # W42 by hand: 16 real trades; exact: day-stop row 3 (rule 4, +$40), alt rows 5 (rule 2, −$600), 8 (rules 2 and 4,
    # open) and 9 (rule 4, +$2); only the approximate rule 4: rows 4 (−$20) and 110 (+$3), apart from the total
    assert lines[1] == "Минулий тиждень 2025-W42 (13.10–19.10): перевірено угод 16, точно порушили хоч одне правило 4, їхній P&amp;L −$558."
    assert lines[2] == (
        "Найчастіше: 4 · денний стоп — 3, 2 · ≤ 3 лонги в альтах — 2. Ще 2 — лише наближене правило 4 (без часу відкриття, "
        "оцінено за часом закриття), P&amp;L −$17: у «точно порушили» не входять."
    )
    # W43: the open 0DTE option bought 7 h before the 08:00 UTC delivery breaks rule 8; no P&L yet
    assert lines[3] == "Цей тиждень 2025-W43 (з 20.10): перевірено угод 1, точно порушили хоч одне правило 1."
    assert lines[4] == "Найчастіше: 8 · опціони — 1."
    # the numbers are analytics.rules_check's and the dashboard's copy on the same real trades (no paper, no airdrop)
    real = rw.real_trades(got)
    assert len(real) == 17 and {r["id"] for r in got} - {r["id"] for r in real} == {901, 902}
    ref = {w["week"]: w for w in rc.weekly_violations(real, now_ts=SEND_AT)}
    mine = {w["week"]: w for w in db.rules_weekly(real, SEND_AT)}
    for wk in ("2025-W42", "2025-W43"):
        for k in ("n_trades", "n_violating", "n_violating_approx"):
            assert ref[wk][k] == mine[wk][k]
    assert ref["2025-W42"]["pnl_violating"] == pytest.approx(-558.0) and ref["2025-W42"]["pnl_violating_approx"] == pytest.approx(-17.0)
    for part in ("Не порада", "нічого не торгує", "$10 000"):
        assert part in rw.CAVEAT
    assert text == rw.build_summary(got, SEND_AT)  # pure


def test_quiet_weeks_point_to_the_last_week_with_trades():
    rows = _shift(trc._day_stop_rows(), 7)  # W42 only
    text = rw.build_summary(rows, SEND_AT + 14 * DAY)  # Monday of W45
    lines = text.splitlines()
    assert lines[1] == "Минулий тиждень 2025-W44 (27.10–02.11): угод не відкривали."
    assert lines[2] == "Цей тиждень 2025-W45 (з 03.11): угод не відкривали."
    assert lines[3] == "Останній тиждень з угодами — 2025-W42: перевірено угод 6, точно порушили 1."
    clean = [
        trc._t(1, "BTCUSDT", "long", SEND_AT - 2 * DAY, SEND_AT - 2 * DAY + H, 5.0, size=0.01, entry=80000.0, stop=79000.0, target=82000.0)
    ]
    assert "перевірено угод 1, точних порушень немає." in rw.build_summary(clean, SEND_AT)


def test_empty_missing_and_old_stores(tmp_path):
    t = SEND_AT
    empty, err = rw.load_journal(_store(tmp_path / "e.sqlite", 23))
    assert empty == [] and err is None
    assert "Угод ще немає" in rw.build_summary(empty, t)
    missing = tmp_path / "none.sqlite"
    assert rw.load_journal(missing) == (None, "missing") and not missing.exists()
    assert "файла бази ще немає" in rw.build_summary(None, t, "missing")
    assert rw.load_journal(_store(tmp_path / "v2.sqlite", 2)) == (None, "no_table")
    assert "таблиці <code>journal</code> у базі ще немає" in rw.build_summary(None, t, "no_table")
    bad = rw.load_journal(tmp_path)  # a directory: sqlite cannot read it
    assert bad[0] is None and bad[1].startswith("unreadable: ")
    paper = [r for r in _journal() if r["id"] in (901, 902)]
    assert "Реальних угод немає: у журналі лише паперові чи airdrop-рядки (2)." in rw.build_summary(paper, t)
    cancelled = [{**trc._t(1, "SOL", "long", t - DAY, None, None), "status": "cancelled"}]
    assert "Відкритих чи закритих угод з часом відкриття немає" in rw.build_summary(cancelled, t)
    for text in (rw.build_summary(None, t, "missing"), rw.build_summary(empty, t), rw.build_summary(paper, t)):
        assert len(text.splitlines()) <= 8 and "Не порада" in text


def test_untrusted_text_is_escaped_and_odd_rows_never_raise():
    text = rw.build_summary(None, SEND_AT, "unreadable: <b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "<b>x</b>" not in text
    sym = [trc._t(1, "<script>alert(1)</script>", "long", SEND_AT - DAY, SEND_AT - DAY + H, -5.0, '["<b>"]')]
    assert "<script" not in rw.build_summary(sym, SEND_AT)
    odd = [{"id": 1, "status": "closed", "opened_ts": 1e20, "closed_ts": 1e20, "symbol": "SOL", "side": "long", "tags": "[]"}]
    assert "Звіт не вдалося зібрати: несподіваний формат даних (" in rw.build_summary(odd, SEND_AT)


def test_main_preview_and_send(tmp_path, monkeypatch, capsys):
    path = _store(tmp_path / "t.sqlite", 23, _journal())
    monkeypatch.setattr(rw.time, "time", lambda: SEND_AT)

    def boom(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(tg_send, "send", boom)
    assert rw.main(["--db", str(path), "--print"]) == 0
    out = capsys.readouterr().out
    assert "перевірено угод 16," in out and "[buttons: none (no $TRADEDESK_PANEL_URL" in out
    assert rw.main(["--db", str(path), "--out", str(tmp_path / "o")]) == 0
    assert "Правила власника за тиждень" in (tmp_path / "o" / "rules.html").read_text(encoding="utf-8")
    sent = []
    monkeypatch.setenv(rw.ab.PANEL_URL_ENV, "https://tradedesk.tail1234.ts.net/")
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert rw.main(["--db", str(path)]) == 0
    assert sent[0][1]["topic"] == "alerts" and sent[0][1]["buttons"] == [("Журнал", "https://tradedesk.tail1234.ts.net/journal")]
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or False)  # Telegram down
    assert rw.main(["--db", str(tmp_path / "none.sqlite")]) == 1 and "файла бази ще немає" in sent[-1][0]


def test_flag_path_matches_between_the_unit_and_step_27():
    unit = (ROOT / "deploy" / "user-units" / "tradedesk-rules-weekly.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "user-units" / "tradedesk-rules-weekly.timer").read_text(encoding="utf-8")
    step = (ROOT / "scripts" / "server" / "27-rules-weekly-timer.sh").read_text(encoding="utf-8")
    cond = re.search(r"^ConditionPathExists=(\S+)$", unit, re.M).group(1)
    d = re.search(r"^DIR=(\S+)$", step, re.M).group(1)
    flag = re.search(r'^FLAG="\$DIR/(\S+)"$', step, re.M).group(1)
    assert cond == f"{d}/{flag}" and flag == "rules-telegram.on"  # its own switch, not the brief's, radar's or forecast's
    assert "scripts/rules_weekly_tg.py" in unit and re.search(r"^OnCalendar=Mon \*-\*-\* 09:00:00 Europe/Kyiv$", timer, re.M)
    assert "runuser" not in step and "sudo -" not in step and '"$(id -un)" == "$SVC"' in step  # runs as tradedesk
    assert re.search(r"^DB=(\S+)$", step, re.M).group(1) == "/var/lib/tradedesk-mcp/tradedesk.sqlite"  # dashboard.JOURNAL_DB on the server
