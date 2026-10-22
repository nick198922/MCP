"""Weekly review of the owner's journal trades: analytics.journal.weekly_review and scripts/journal_weekly_tg.py; no network."""

import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402
import journal_weekly_tg as jw  # noqa: E402
import tg_send  # noqa: E402

from tradedesk.analytics import journal as jr  # noqa: E402
from tradedesk.store.schema import MIGRATIONS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAY, H = 86400.0, 3600.0
SEND_AT = datetime(2025, 10, 19, 17, tzinfo=UTC).timestamp()  # Sunday 20:00 Kyiv (EEST): ISO week 2025-W42 ends tonight
COLS = (
    "id", "opened_ts", "closed_ts", "symbol", "side", "size", "entry", "stop", "target", "thesis", "tags", "status",
    "exit", "outcome", "pnl", "r_multiple", "note",
)  # fmt: skip
BYBIT, BINANCE, HL = '["bybit", "import", "linear"]', '["binance", "import", "usdm"]', '["hyperliquid", "import", "perp"]'
BOT = '["demo-paper", "bybit-demo", "setup:S3", "tf:4h", "auto"]'


@pytest.fixture(autouse=True)
def _no_dashboard_url(tmp_path, monkeypatch):
    """The server's real URL file (step 23), key file or a set $TRADEDESK_PANEL_URL must not change these tests."""
    monkeypatch.setattr(jw.ab, "DASHBOARD_URL_PATH", tmp_path / "no_dashboard_url")
    monkeypatch.setattr(db, "KEY_FILE", tmp_path / "no_dashboard.key")
    monkeypatch.delenv(jw.ab.PANEL_URL_ENV, raising=False)


def _ts(d: int, h: float = 10.0) -> int:
    return int(datetime(2025, 10, d, tzinfo=UTC).timestamp() + h * H)


def _t(i, sym, side, opened, closed, pnl, tags, *, status="closed", stop=None, r=None, outcome=None, **kw) -> dict:
    """A row as the importers, journal_add / /trade + /close and the demo trader write it (outcome set on close)."""
    if outcome is None and status == "closed" and pnl is not None:
        outcome = jr.outcome_of(pnl)
    return {
        "id": i, "opened_ts": opened, "closed_ts": closed, "symbol": sym, "side": side, "size": kw.get("size", 1.0),
        "entry": kw.get("entry", 100.0), "stop": stop, "target": kw.get("target"), "thesis": kw.get("thesis"), "tags": tags,
        "status": status, "exit": kw.get("exit"), "outcome": outcome, "pnl": pnl, "r_multiple": r, "note": kw.get("note"),
    }  # fmt: skip


def _journal() -> list[dict]:
    """Shaped like the live journal of 23.10: venue imports without stops (Bybit linear rows have opened = closed),
    a bought Bybit option, a Hyperliquid airdrop row, demo-bot rows with R, a paper row and the owner's own trades
    from /trade + /close (one with a stop and R, one option without R), plus an open and a cancelled row."""
    return [
        # last week, 2025-W41 (06.10-12.10)
        _t(1, "BTCUSDT", "long", _ts(7), _ts(7), 120.0, BYBIT),
        _t(2, "SOLUSDT", "short", _ts(8), _ts(8), -45.0, BYBIT),
        _t(3, "ETHUSDT", "long", _ts(8, 9), _ts(9, 12), 30.0, BINANCE),
        _t(4, "HYPE", "long", _ts(9), _ts(10), -80.0, HL),
        _t(5, "PURR", "long", _ts(9), _ts(10), 500.0, '["hyperliquid", "import", "perp", "airdrop"]'),
        _t(6, "BTC-10OCT25-120000-C", "long", _ts(9), _ts(10, 8), -25.0, '["bybit", "import", "option"]', size=0.01, entry=2500.0),
        _t(7, "AVAX/USDT:USDT", "long", _ts(11), _ts(11, 16), -93.9, BOT, stop=9.047, r=-0.96),
        _t(8, "SOLUSDT", "long", _ts(12, 9), _ts(12, 15), 30.0, '["telegram"]', size=2.0, entry=200.0, stop=190.0, r=1.5, exit=215.0),
        _t(9, "BTC-PP", "long", _ts(9), _ts(9, 12), -999.0, '["pp-paper"]'),
        _t(10, "DOGEUSDT", "long", _ts(9), _ts(9, 12), 777.0, '["auto"]'),  # a bot row without the demo-paper tag
        # this week, 2025-W42 (13.10-19.10)
        _t(11, "XRPUSDT", "long", _ts(14), _ts(14), 12.4, BYBIT),
        _t(12, "ETH-17OCT25-4000-C", "long", _ts(15), _ts(16), -60.0, '["telegram", "option"]', size=0.5, entry=120.0, stop=3900.0),
        _t(13, "BTCUSDT", "long", _ts(18), None, None, '["telegram"]', status="open", stop=104000.0),
        _t(14, "ARBUSDT", "long", _ts(17), _ts(17, 11), None, '["telegram"]', status="cancelled", outcome="cancelled"),
        _t(15, "LINK/USDT:USDT", "short", _ts(17), _ts(18), 226.5, BOT, stop=12.9, r=2.27),
    ]


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


# ---------- pure analytics ----------


def test_weekly_review_by_close_week():
    real = jw.real_trades(_journal())
    assert sorted(t["id"] for t in real) == [1, 2, 3, 4, 6, 8, 11, 12, 13, 14]  # no airdrop, paper or bot rows
    rev = jr.weekly_review(real, now_ts=SEND_AT)
    last, this = rev["last"], rev["this"]
    assert last["start_ts"] == _ts(6, 0) and last["end_ts"] == this["start_ts"] == _ts(13, 0) and this["end_ts"] == _ts(20, 0)
    # W41 by hand: 1 +120, 2 −45, 3 +30, 4 −80, 6 −25, 8 +30 → 6 trades, 3 wins, +$30, +$5 per trade; R only on #8
    assert last["closed_trades"] == 6 and last["wins"] == 3 and last["win_rate"] == pytest.approx(0.5)
    assert last["total_pnl"] == pytest.approx(30.0) and last["expectancy_per_trade"] == pytest.approx(5.0)
    assert last["r_count"] == 1 and last["avg_r"] == pytest.approx(1.5) and last["small_sample"]
    assert last["best"]["id"] == 1 and last["worst"]["id"] == 4
    assert [(s["source"], s["n"]) for s in last["by_source"]][:2] == [("Bybit", 2), ("Binance", 1)]
    assert {s["source"]: s["pnl"] for s in last["by_source"]} == pytest.approx(
        {"Bybit": 75.0, "Binance": 30.0, "вручну": 30.0, "Bybit опціони": -25.0, "Hyperliquid": -80.0}
    )
    # W42: #11 +12.4 and the /close option #12 −60 (no R: its stop is the underlying's price); open and cancelled out
    assert this["closed_trades"] == 2 and this["total_pnl"] == pytest.approx(-47.6) and this["avg_r"] is None and this["r_count"] == 0
    assert rev["latest"] is None


def test_weekly_review_edges():
    assert jr.week_start(SEND_AT) == _ts(13, 0) and jr.week_start(_ts(13, 0)) == _ts(13, 0)
    empty = jr.weekly_review([], now_ts=SEND_AT)
    assert empty["last"]["closed_trades"] == 0 and empty["last"]["best"] is None and empty["latest"] is None
    # a closed row without closed_ts falls back to opened_ts; a missing outcome comes from the P&L; odd values are left out
    rows = [
        {"id": 1, "status": "closed", "opened_ts": _ts(7), "closed_ts": None, "pnl": 10.0, "symbol": "X", "outcome": None},
        {"id": 2, "status": "closed", "opened_ts": _ts(7), "closed_ts": _ts(8), "pnl": float("nan")},
        {"id": 3, "status": "closed", "opened_ts": "x", "closed_ts": None, "pnl": 5.0},
        {"id": 4, "status": "closed", "opened_ts": _ts(8), "closed_ts": _ts(8), "pnl": True},
        {"id": 5, "status": "closed", "opened_ts": _ts(8), "closed_ts": _ts(8), "pnl": -3.0, "r_multiple": float("inf")},
    ]
    w = jr.weekly_review(rows, now_ts=SEND_AT)["last"]
    assert w["closed_trades"] == 2 and w["wins"] == 1 and w["r_count"] == 0 and w["by_source"] == [{"source": "?", "n": 2, "pnl": 7.0}]
    # both weeks quiet: the latest earlier week with a close
    late = jr.weekly_review(rows, now_ts=SEND_AT + 21 * DAY)
    assert late["last"]["closed_trades"] == 0 and late["latest"]["start_ts"] == _ts(6, 0) and late["latest"]["closed_trades"] == 2
    many = [_t(i, "BTCUSDT", "long", _ts(7), _ts(7), 1.0, BYBIT) for i in range(jr.SMALL_SAMPLE)]
    assert not jr.weekly_review(many, now_ts=SEND_AT)["last"]["small_sample"]


# ---------- the Telegram message ----------


def test_summary_on_a_real_shaped_journal(tmp_path):
    path = _store(tmp_path / "t.sqlite", 23, _journal())
    before = path.read_bytes()
    got, err = jw.load_journal(path)
    assert err is None and len(got) == 15
    assert path.read_bytes() == before  # read-only
    text = jw.build_summary(got, SEND_AT)
    lines = text.splitlines()
    assert len(lines) <= 8 and len(text) < 2000, text
    assert lines[0].startswith("<b>Журнал угод за тиждень · 19.10 20:00") and lines[-1] == f"<i>{jw._h(jw.CAVEAT)}</i>"
    assert lines[1] == (
        "Минулий тиждень 2025-W41 (06.10–12.10): закрито 6 (замало для висновку), у плюс 3 (50 %), P&amp;L +$30, "
        "на угоду +$5; середній R +1.50 за 1 з 6 угод зі стопом."
    )
    assert lines[2] == (
        "Найкраща: BTCUSDT +$120 (#1), найгірша: HYPE −$80 (#4). "
        "За джерелом: Bybit 2 (+$75), Binance 1 (+$30), вручну 1 (+$30), Bybit опціони 1 (−$25), ще 1."
    )
    assert lines[3] == (
        "Цей тиждень 2025-W42 (з 13.10): закрито 2 (замало для висновку), у плюс 1 (50 %), P&amp;L −$48, на угоду −$24; "
        "R невідомо (стопу в цих угодах немає)."
    )
    assert (
        lines[4] == "Найкраща: XRPUSDT +$12 (#11), найгірша: ETH-17OCT25-4000-C −$60 (#12). За джерелом: Bybit 1 (+$12), вручну 1 (−$60)."
    )
    # the excluded rows leave no trace: no airdrop +500, paper −999, bot +777 / +226.5
    for gone in ("PURR", "BTC-PP", "DOGE", "AVAX", "LINK", "$500", "$999", "$777"):
        assert gone not in text
    for part in ("Не порада", "жодних ордерів", "менше 30 угод"):
        assert part in jw.CAVEAT
    assert text == jw.build_summary(got, SEND_AT)  # pure


def test_r_less_and_quiet_weeks():
    imports = [r for r in _journal() if r["id"] in (1, 2, 3, 4, 6)]  # venue imports: no stop, no R
    lines = jw.build_summary(imports, SEND_AT).splitlines()
    assert lines[1].endswith(
        "закрито 5 (замало для висновку), у плюс 2 (40 %), P&amp;L $0, на угоду $0; R невідомо (стопу в цих угодах немає)."
    )
    assert lines[3] == "Цей тиждень 2025-W42 (з 13.10): угод не закривали."
    lines = jw.build_summary(imports, SEND_AT + 14 * DAY).splitlines()  # Sunday of W44
    assert lines[1] == "Минулий тиждень 2025-W43 (20.10–26.10): угод не закривали."
    assert lines[2] == "Цей тиждень 2025-W44 (з 27.10): угод не закривали."
    assert lines[3] == "Останній тиждень із закритими угодами — 2025-W41: закрито 5, P&amp;L $0."
    one = [_t(1, "BTCUSDT", "long", _ts(7), _ts(7), 120.0, BYBIT)]
    assert "Єдина: BTCUSDT +$120 (#1). За джерелом: Bybit 1 (+$120)." in jw.build_summary(one, SEND_AT)
    many = [_t(i, "BTCUSDT", "long", _ts(7), _ts(7), 1.0, BYBIT) for i in range(30)]
    assert "закрито 30, у плюс 30 (100 %)" in jw.build_summary(many, SEND_AT)  # no small-sample mark from 30 on


def test_empty_missing_and_old_stores(tmp_path):
    t = SEND_AT
    empty, err = jw.load_journal(_store(tmp_path / "e.sqlite", 23))
    assert empty == [] and err is None
    assert "Угод ще немає" in jw.build_summary(empty, t)
    missing = tmp_path / "none.sqlite"
    assert jw.load_journal(missing) == (None, "missing") and not missing.exists()
    assert "файла бази ще немає" in jw.build_summary(None, t, "missing")
    assert jw.load_journal(_store(tmp_path / "v2.sqlite", 2)) == (None, "no_table")
    assert "таблиці <code>journal</code> у базі ще немає" in jw.build_summary(None, t, "no_table")
    bad = jw.load_journal(tmp_path)  # a directory: sqlite cannot read it
    assert bad[0] is None and bad[1].startswith("unreadable: ")
    fake = [r for r in _journal() if r["id"] in (5, 7, 9, 10, 15)]
    assert "Реальних угод немає: у журналі лише паперові, airdrop-рядки чи угоди демо-бота (5)." in jw.build_summary(fake, t)
    for text in (jw.build_summary(None, t, "missing"), jw.build_summary(empty, t), jw.build_summary(fake, t)):
        assert len(text.splitlines()) <= 8 and "Не порада" in text


def test_untrusted_text_is_escaped_and_odd_rows_never_raise():
    text = jw.build_summary(None, SEND_AT, "unreadable: <b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "<b>x</b>" not in text
    evil = "<script>‮TSUB​\nalert(1)</script>" + "A" * 50
    rows = [_t(1, evil, "long", _ts(7), _ts(7), -5.0, '["<b>"]', note="<i>note</i>", thesis="<a href=x>t</a>")]
    text = jw.build_summary(rows, SEND_AT)
    assert "<script" not in text and "‮" not in text and "​" not in text and "&lt;script&gt;" in text
    assert "note" not in text and "href" not in text and len(text.splitlines()) <= 8  # notes and theses are never shown
    assert jw.clean(evil).endswith("…") and len(jw.clean("x" * 100)) == jw.SYMBOL_SHOWN
    assert jw.clean(None) == "?" and jw.clean("\u0000⁦") == "?"
    far = [{"id": 1, "status": "closed", "opened_ts": 1e20, "closed_ts": 1e20, "pnl": 1.0, "symbol": "SOL", "tags": "[]"}]
    assert "Цей тиждень 2025-W42 (з 13.10): угод не закривали." in jw.build_summary(far, SEND_AT)  # far future: no week
    odd = [{"id": 1, "status": "closed", "pnl": 1.0, "tags": "[]"}, "not a row"]
    assert "Розбір не вдалося зібрати: несподіваний формат даних (" in jw.build_summary(odd, SEND_AT)


def test_main_preview_and_send(tmp_path, monkeypatch, capsys):
    path = _store(tmp_path / "t.sqlite", 23, _journal())
    monkeypatch.setattr(jw.time, "time", lambda: SEND_AT)

    def boom(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(tg_send, "send", boom)
    assert jw.main(["--db", str(path), "--print"]) == 0
    out = capsys.readouterr().out
    assert "закрито 6 (замало для висновку)" in out and "[buttons: none (no $TRADEDESK_PANEL_URL" in out
    assert jw.main(["--db", str(path), "--out", str(tmp_path / "o")]) == 0
    assert "Журнал угод за тиждень" in (tmp_path / "o" / "journal.html").read_text(encoding="utf-8")
    sent = []
    monkeypatch.setenv(jw.ab.PANEL_URL_ENV, "https://tradedesk.tail1234.ts.net/")
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert jw.main(["--db", str(path)]) == 0
    assert sent[0][1]["topic"] == "alerts" and sent[0][1]["buttons"] == [("Журнал", "https://tradedesk.tail1234.ts.net/journal")]
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or False)  # Telegram down
    assert jw.main(["--db", str(tmp_path / "none.sqlite")]) == 1 and "файла бази ще немає" in sent[-1][0]


def test_flag_path_matches_between_the_unit_and_step_28():
    unit = (ROOT / "deploy" / "user-units" / "tradedesk-journal-weekly.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "user-units" / "tradedesk-journal-weekly.timer").read_text(encoding="utf-8")
    step = (ROOT / "scripts" / "server" / "28-journal-weekly-timer.sh").read_text(encoding="utf-8")
    cond = re.search(r"^ConditionPathExists=(\S+)$", unit, re.M).group(1)
    d = re.search(r"^DIR=(\S+)$", step, re.M).group(1)
    flag = re.search(r'^FLAG="\$DIR/(\S+)"$', step, re.M).group(1)
    assert cond == f"{d}/{flag}" and flag == "journal-telegram.on"  # its own switch, not the rules' or the forecast's
    assert "scripts/journal_weekly_tg.py" in unit and re.search(r"^OnCalendar=Sun \*-\*-\* 20:00:00 Europe/Kyiv$", timer, re.M)
    assert "runuser" not in step and "sudo -" not in step and '"$(id -un)" == "$SVC"' in step  # runs as tradedesk
    assert re.search(r"^DB=(\S+)$", step, re.M).group(1) == "/var/lib/tradedesk-mcp/tradedesk.sqlite"  # dashboard.JOURNAL_DB on the server
