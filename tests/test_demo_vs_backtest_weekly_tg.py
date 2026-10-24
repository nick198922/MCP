"""Weekly «demo vs backtest» summary for Telegram (scripts/demo_vs_backtest_weekly_tg.py); no network."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402
import demo_vs_backtest_weekly_tg as dw  # noqa: E402
import tg_send  # noqa: E402

from tradedesk.analytics import demo_vs_backtest as dvb  # noqa: E402
from tradedesk.core.envelope import Evidence, Fetched  # noqa: E402
from tradedesk.store.db import Store  # noqa: E402
from tradedesk.store.schema import MIGRATIONS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((ROOT / "tests" / "fixtures" / "demo_vs_backtest_bybit_2026-10-23.json").read_text())
SEND_AT = datetime(2026, 10, 25, 18, 30, tzinfo=UTC).timestamp()  # Sunday 20:30 Kyiv (EET after 25.10 04:00)


@pytest.fixture(autouse=True)
def _no_dashboard_url(tmp_path, monkeypatch):
    """The server's real URL file (step 23), key file or a set $TRADEDESK_PANEL_URL must not change these tests."""
    monkeypatch.setattr(dw.ab, "DASHBOARD_URL_PATH", tmp_path / "no_dashboard_url")
    monkeypatch.setattr(db, "KEY_FILE", tmp_path / "no_dashboard.key")
    monkeypatch.delenv(dw.ab.PANEL_URL_ENV, raising=False)


def _x(verdict: str, setup: str = "S3", day: int = 24, r_demo=1.0, r_bt=0.8, reasons=(), edge=None) -> dvb.Replay:
    """A replay as analytics.demo_vs_backtest.replay returns it, closed at 12:00 UTC on October ``day``."""
    closed = int(datetime(2026, 10, day, 12, tzinfo=UTC).timestamp() * 1000)
    diff = None if r_demo is None or r_bt is None else r_demo - r_bt
    return dvb.Replay(
        f"td-{setup}-{day}", "DOT/USDT:USDT", setup, "4h", "long", closed - 86_400_000, closed, verdict, tuple(reasons),
        r_demo=r_demo, r_demo_bt=r_demo, r_bt=r_bt, r_diff=diff, entry_edge_r=edge,
    )  # fmt: skip


def _rows() -> list[dvb.Replay]:
    """Two S3 matches, an S14 mismatch (entry and R) this week, an S14 'no signal' and an option trade closed before
    the week, and an S16 trade the backtest has not resolved yet."""
    return [
        _x("match", day=24, edge=0.1),
        _x("match", day=20, r_demo=-1.0, r_bt=-1.0, edge=-0.3),
        _x("mismatch", "S14", day=22, r_demo=2.0, r_bt=1.0, reasons=("entry", "r"), edge=-0.4),
        _x("no_signal", "S14", day=10, r_bt=None),
        _x("pending", "S16", day=25, r_bt=None),
        _x("unsupported", "OPT", day=12, r_demo=None, r_bt=None),
    ]


def test_summary_happy_path_below_thirty():
    text = dw.build_summary(_rows(), SEND_AT)
    lines = text.splitlines()
    assert len(lines) <= 8 and len(text) < 2000, text
    assert lines[0].startswith("<b>Демо проти бектесту за тиждень · 25.10 20:30") and lines[-1] == f"<i>{dw._h(dw.CAVEAT)}</i>"
    assert lines[1] == (
        "Від першої демо-угоди (з 10.10): закрито 6, звірено 4: збіг 2, немає сигналу 1, розбіжність 1; "
        "не вирішено: бектест ще відкритий 1, не відтворюється 1."
    )
    assert lines[2] == (
        "Висновок: замало для висновку (звірено 4, потрібно ≥ 30), числа — лише опис. Середнє R в одиницях ризику бектесту "
        "(угод з обома R: 3): демо +0.67, бектест +0.27, різниця +0.40. Вхід демо проти закриття свічки: −0.20R у середньому "
        "(+ — краще)."
    )
    assert lines[3] == (
        "За сетапами: S14: збіг 0, немає сигналу 1, розбіжність 1; S3: збіг 2, немає сигналу 0, розбіжність 0; "
        "OPT: збіг 0, немає сигналу 0, розбіжність 0, не вирішено 1; S16: збіг 0, немає сигналу 0, розбіжність 0, "
        "не вирішено 1. Причини розбіжностей: вхід 1, R 1."
    )
    # the week is the last 7 days: the S14 'no signal' (10.10) and the option (12.10) stay in the running total only
    assert lines[4] == (
        "За тиждень 18.10–25.10: закрито 4, звірено 3: збіг 2, немає сигналу 0, розбіжність 1; не вирішено: бектест ще відкритий 1."
    )
    for part in ("Не порада", "нічого не торгує", "менше 30 звірених угод", "+30 % ROE"):
        assert part in dw.CAVEAT
    assert text == dw.build_summary(_rows(), SEND_AT)  # pure: time is a parameter
    quiet = dw.build_summary(_rows(), SEND_AT + 14 * 86400).splitlines()
    assert quiet[4] == "За тиждень 01.11–08.11: демо-угод не закривали." and quiet[1] == lines[1]


def test_summary_thirty_decided_gives_the_conclusion():
    rows = [_x("match", r_demo=0.1 * (i % 3)) for i in range(27)] + [_x("no_signal", day=23, r_bt=None)] * 3
    text = dw.build_summary(rows, SEND_AT)
    assert "Висновок: збіг із бектестом 90% угод (27 з 30); середня різниця R (демо − бектест)" in text
    assert "замало для висновку (" not in text and len(text.splitlines()) <= 8
    # 29 decided plus undecided ones: still too few
    few = [_x("match")] * 29 + [_x("pending", r_bt=None)] * 5
    assert "замало для висновку (звірено 29, потрібно ≥ 30)" in dw.build_summary(few, SEND_AT)


def test_empty_states():
    for err, part in (
        ("missing", "файла бази ще немає"),
        ("no_table", "таблиці <code>demo_trade</code> ще немає: база до схеми v19"),
        ("replay failed: RuntimeError", "звірку не вдалося виконати (replay failed: RuntimeError)"),
        (None, "звірку не вдалося виконати (невідома причина)"),
    ):
        text = dw.build_summary(None, SEND_AT, err)
        assert f"<b>Звірки немає:</b> {part}" in text and len(text.splitlines()) <= 8 and "Не порада" in text
    text = dw.build_summary([], SEND_AT)
    assert "<b>Закритих демо-угод ще немає:</b>" in text and "Висновок" not in text


def test_hostile_text_is_escaped_and_odd_rows_never_raise():
    text = dw.build_summary(None, SEND_AT, "unreadable: <b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "<b>x</b>" not in text
    evil = "<script>‮S1​\nalert(1)</script>"
    rows = [_x("mismatch", evil, reasons=("<i>odd</i>",)), _x("match", "a&b")]
    text = dw.build_summary(rows, SEND_AT, None, ["ohlcv:bybit:<b>DOT</b>‮: network"])
    assert "<script" not in text and "‮" not in text and "​" not in text and "<i>odd" not in text
    assert "&lt;script&gt;" in text and "a&amp;b:" in text and "&lt;i&gt;odd&lt;/i&gt; 1" in text
    assert "Не все завантажилось (1), перше: ohlcv:bybit:&lt;b&gt;DOT&lt;/b&gt; : network." in text
    assert len(text.splitlines()) <= 8
    assert "несподіваний формат даних (" in dw.build_summary(["not a replay"], SEND_AT)  # type: ignore[list-item]


# ---------------------------------------------------------------- reading the store and main


def _store(tmp_path: Path, trades: list[dict[str, Any]] = ()) -> Path:
    path = tmp_path / "t.sqlite"

    async def go() -> None:
        st = Store(str(path))
        await st.init()
        for r in trades:
            await st.demo_trade_insert({**r, "venue": "bybit-demo", "qty": 1.0, "leverage": 10, "comment": "c"})
        await st.aclose()

    asyncio.run(go())
    return path


class _FixtureExchanges:
    async def ohlcv(self, venue: str, symbol: str, tf: str, limit: int, since: int, until: int) -> Any:
        key = "bars" if tf == "4h" else "path"
        rows = next(t[key] for t in FIX["trades"] if t["row"]["symbol"] == symbol)
        return Fetched([x for x in rows if since <= x[0] <= until][:limit], Evidence(provider="fixture", tool="ohlcv"))


class _FixtureRuntime:
    def __init__(self, *_: Any) -> None:
        self.exchanges = _FixtureExchanges()

    async def aclose(self) -> None:
        pass


def test_probe_and_collect(tmp_path, monkeypatch):
    monkeypatch.setattr(dw.dv, "Runtime", _FixtureRuntime)
    missing = tmp_path / "none.sqlite"
    assert dw.probe(missing) == "missing" and dw.collect(missing, "bybit", SEND_AT) == (None, "missing", []) and not missing.exists()
    old = tmp_path / "v18.sqlite"
    con = sqlite3.connect(old)
    for v in sorted(MIGRATIONS):
        if v <= 18:
            for sql in MIGRATIONS[v]:
                con.execute(sql)
    con.commit()
    con.close()
    assert dw.probe(old) == "no_table"
    assert dw.probe(tmp_path).startswith("unreadable: ")  # a directory
    (tmp_path / "e").mkdir()
    empty = _store(tmp_path / "e")
    assert dw.probe(empty) is None and dw.collect(empty, "bybit", SEND_AT) == ([], None, [])


def test_main_replays_the_real_sample_and_sends_to_the_demo_topic(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dw.dv, "Runtime", _FixtureRuntime)
    path = _store(tmp_path, [t["row"] for t in FIX["trades"]])
    before = path.read_bytes()
    now = FIX["now_ms"] / 1000
    monkeypatch.setattr(dw.time, "time", lambda: now)

    def boom(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(tg_send, "send", boom)
    assert dw.main(["--db", str(path), "--print"]) == 0
    out = capsys.readouterr().out
    assert "закрито 2, звірено 2: збіг 2, немає сигналу 0, розбіжність 0." in out and "замало для висновку" in out
    assert "За сетапами: S3: збіг 2" in out and "[buttons: none (no $TRADEDESK_PANEL_URL" in out
    assert path.read_bytes() == before  # read-only
    assert dw.main(["--db", str(path), "--out", str(tmp_path / "o")]) == 0
    assert "Демо проти бектесту" in (tmp_path / "o" / "demo_vs_backtest.html").read_text(encoding="utf-8")
    sent = []
    monkeypatch.setenv(dw.ab.PANEL_URL_ENV, "https://tradedesk.tail1234.ts.net/")
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert dw.main(["--db", str(path)]) == 0
    assert sent[0][1]["topic"] == "demo" and sent[0][1]["buttons"] == [("Журнал", "https://tradedesk.tail1234.ts.net/journal")]
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or False)  # Telegram down
    assert dw.main(["--db", str(tmp_path / "none.sqlite")]) == 1 and "файла бази ще немає" in sent[-1][0]


def test_flag_path_matches_between_the_unit_and_step_29():
    unit = (ROOT / "deploy" / "user-units" / "tradedesk-demo-vs-backtest-weekly.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "user-units" / "tradedesk-demo-vs-backtest-weekly.timer").read_text(encoding="utf-8")
    step = (ROOT / "scripts" / "server" / "29-demo-vs-backtest-weekly-timer.sh").read_text(encoding="utf-8")
    cond = re.search(r"^ConditionPathExists=(\S+)$", unit, re.M).group(1)
    d = re.search(r"^DIR=(\S+)$", step, re.M).group(1)
    flag = re.search(r'^FLAG="\$DIR/(\S+)"$', step, re.M).group(1)
    assert cond == f"{d}/{flag}" and flag == "demo-vs-backtest-telegram.on"  # its own switch
    assert "scripts/demo_vs_backtest_weekly_tg.py" in unit and re.search(r"^OnCalendar=Sun \*-\*-\* 20:30:00 Europe/Kyiv$", timer, re.M)
    assert "runuser" not in step and "sudo -" not in step and '"$(id -un)" == "$SVC"' in step  # runs as tradedesk
    assert re.search(r"^DB=(\S+)$", step, re.M).group(1) == dw.dv.DEFAULT_DB  # the demo trader's DB on the server
