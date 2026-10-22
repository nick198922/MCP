"""Weekly forecast-log summary for Telegram (scripts/forecast_weekly_tg.py); no network."""

import json
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402
import forecast_weekly_tg as fw  # noqa: E402
import tg_send  # noqa: E402

from tradedesk.analytics import forecast_log as fl  # noqa: E402
from tradedesk.analytics.options import build_chain, build_smile, group_by_expiry  # noqa: E402
from tradedesk.store.schema import MIGRATIONS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "deribit_book_7d_2026-10-22.json"  # real Deribit books, 22.10 03:18 UTC
SAMPLE_NOW = datetime(2026, 10, 22, 3, 18, 58, tzinfo=UTC)
FIRST_EXPIRY = datetime(2026, 10, 30, 8, tzinfo=UTC)
COLS = tuple("day issued_ts asset kind expiry_ts spot forward atm_iv t_years lo hi prob ref_prob rv30 prob_rv".split())  # Store's
SEND_AT = (FIRST_EXPIRY + timedelta(days=15, hours=4)).timestamp()  # Saturday after the third expiry


@pytest.fixture(autouse=True)
def _no_dashboard_url(tmp_path, monkeypatch):
    """The server's real URL file (step 23), key file or a set $TRADEDESK_PANEL_URL must not change these tests."""
    monkeypatch.setattr(fw.ab, "DASHBOARD_URL_PATH", tmp_path / "no_dashboard_url")
    monkeypatch.setattr(db, "KEY_FILE", tmp_path / "no_dashboard.key")
    monkeypatch.delenv(fw.ab.PANEL_URL_ENV, raising=False)


def _smiles() -> dict:
    """The real 22.10 smile and index of BTC and ETH on the 30.10 expiry."""
    out = {}
    book = json.loads(FIXTURE.read_text())
    for asset in ("BTC", "ETH"):
        by = group_by_expiry(build_chain(book[asset], SAMPLE_NOW))
        rows = by[FIRST_EXPIRY]
        out[asset] = (build_smile(rows), next(x.index for x in rows if x.index))
    return out


def _forecasts(n_weeks: int, *, rv: float | None = 0.40) -> list[dict]:
    """Rows as the collector writes them (analytics.forecast_log.build_forecasts on the real smiles): one weekly expiry
    per week, two issue days each, BTC and ETH; settled with outcome()/brier() on a delivery that alternates
    +2 %, -7 %, +12 % of the spot. The newest expiry (week n_weeks) is left open."""
    rows = []
    moves = (1.02, 0.93, 1.12)
    for asset, (sm, spot) in _smiles().items():
        for w in range(n_weeks + 1):
            exp = FIRST_EXPIRY + timedelta(weeks=w)
            for back in (7, 5):
                for r in fl.build_forecasts(sm, asset=asset, spot=spot, expiry=exp, now=exp - timedelta(days=back), rv=rv):
                    if w < n_weeks:
                        s = spot * moves[w % 3]
                        o = fl.outcome(r["lo"], r["hi"], s)
                        r.update(status="settled", delivery_price=s, outcome=o, brier=fl.brier(r["prob"], o))
                    rows.append(r)
    return rows


def _store(path: Path, version: int, rows: list[dict] = ()) -> Path:
    """A SQLite file migrated to `version` with the real schema, holding `rows` in forecast_log."""
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= version:
            for sql in MIGRATIONS[v]:
                con.execute(sql)
    cols = [c for c in COLS if version >= 23 or c not in ("rv30", "prob_rv")]
    for r in rows:
        extra = {k: r.get(k) for k in ("status", "delivery_price", "outcome", "brier") if r.get(k) is not None}
        names = [*cols, "covered", *extra]
        con.execute(
            f"INSERT INTO forecast_log ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [*(r.get(c) for c in cols), int(bool(r["covered"])), *extra.values()],
        )
    con.commit()
    con.close()
    return path


def _lines(text: str) -> list[str]:
    return text.splitlines()


def test_summary_on_a_real_shaped_store(tmp_path):
    rows = _forecasts(3)
    path = _store(tmp_path / "t.sqlite", 23, rows)
    before = path.read_bytes()
    got, err = fw.load_forecasts(path)
    assert err is None and len(got) == len(rows) == 2 * 4 * 2 * 4
    assert path.read_bytes() == before  # read-only
    text = fw.build_summary(got, SEND_AT)
    lines = _lines(text)
    assert len(lines) <= 8 and len(text) < 2000, text
    assert lines[0].startswith("<b>Журнал прогнозів за тиждень · ") and lines[-1] == f"<i>{fw._h(fw.CAVEAT)}</i>"
    s, c, wk = fl.score(got), fl.compare(got), fl.weekly(got)
    assert s["n"] == 48 and s["n_expiries"] == 6 and c["n_dates"] == 3 and len(wk) == 3
    assert "Розраховано прогнозів: 48, експірацій (актив × дата): 6, відкритих: 16: замало для висновку" in lines[1]
    assert f"Brier опціонів {s['brier']:.3f}" in lines[2]
    assert f"{s['brier_on_ref']:.3f} проти {s['brier_ref']:.3f}" in lines[2]
    assert f"{s['brier']:.3f} проти {s['brier_climatology']:.3f}" in lines[2]
    assert f"Brier {c['brier_risk_neutral']:.3f} проти {c['brier_rv30']:.3f}" in lines[3]
    assert "дат експірації: 3 — лише опис, висновок — від 30 дат." in lines[3] and "ДІ 95 %" in lines[3]
    last = wk[-1]
    assert lines[4].startswith(f"Останній розрахований тиждень {last['week']} (експірація 13.11): прогнозів 16, експірацій 2,")
    assert f"Brier {last['brier']:.3f}" in lines[4]
    for part in ("Не порада", "не прогноз асистента", "ризик-нейтральні"):
        assert part in fw.CAVEAT
    # the dashboard's stdlib copies give the same numbers as the analytics used here
    assert db.fc_score(got)["brier"] == pytest.approx(s["brier"], abs=1e-12)
    assert db.fc_compare(got)["diff"] == pytest.approx(c["diff"], abs=1e-12)
    assert text == fw.build_summary(got, SEND_AT)  # pure


def test_enough_expiries_drops_the_no_conclusion_note():
    rows = _forecasts(31, rv=None)
    for i, r in enumerate(rows):  # a different RV per row so the per-date differences vary
        r["prob_rv"] = 0.3 + (i % 7) / 10
    text = fw.build_summary(rows, SEND_AT + 30 * 7 * 86400)
    assert "замало для висновку" not in text and "числа нижче — опис, перевагу вони не доводять" in text
    assert "дат експірації: 31 — " in text and "лише опис" not in text and "ДІ 95 %" in text
    c = fl.compare(rows)
    lo, hi = c["ci95"]
    want = "точніші опціони" if hi < 0 else "точніша RV30" if lo > 0 else "різниця в межах шуму (ДІ перетинає нуль)"
    assert want in text
    assert len(_lines(text)) <= 8


@pytest.mark.parametrize(
    ("ci", "want"),
    [
        ([-0.03, -0.01], "ДІ нижче нуля: на цій вибірці точніші опціони"),
        ([0.01, 0.03], "ДІ вище нуля: на цій вибірці точніша RV30"),
        ([-0.01, 0.01], "різниця в межах шуму (ДІ перетинає нуль)"),
    ],
)
def test_rv30_verdict_once_enough(ci, want):
    c = {"n": 300, "n_dates": 40, "brier_risk_neutral": 0.2, "brier_rv30": 0.21, "diff": sum(ci) / 2, "ci95": ci, "enough": True}
    line = fw._rv_line(c)
    assert line.endswith(f"дат експірації: 40 — {want}.") and f"ДІ 95 % {fw._signed(ci[0])} … {fw._signed(ci[1])}" in line
    assert "-" not in line  # signs are typographic minus


def test_empty_missing_and_old_stores(tmp_path):
    t = SEND_AT
    empty, err = fw.load_forecasts(_store(tmp_path / "e.sqlite", 23))
    assert empty == [] and err is None
    assert "Прогнозів ще немає" in fw.build_summary(empty, t) and "TRADEDESK_PAPER_FORECAST_LOG_ENABLED=1" in fw.build_summary(empty, t)
    missing = tmp_path / "none.sqlite"
    assert fw.load_forecasts(missing) == (None, "missing") and not missing.exists()
    assert "файла бази ще немає" in fw.build_summary(None, t, "missing")
    assert fw.load_forecasts(_store(tmp_path / "v19.sqlite", 19)) == (None, "no_table")
    assert "база до схеми v20" in fw.build_summary(None, t, "no_table")
    bad = fw.load_forecasts(tmp_path)  # a directory: sqlite cannot read it
    assert bad[0] is None and bad[1].startswith("unreadable: ")
    for text in (fw.build_summary(None, t, "missing"), fw.build_summary(empty, t)):
        assert len(_lines(text)) <= 8 and "Не порада" in text


def test_pre_v23_store_has_no_rv30_pairs(tmp_path):
    rows = _forecasts(2)
    got, err = fw.load_forecasts(_store(tmp_path / "v20.sqlite", 20, rows))
    assert err is None and all(x["prob_rv"] is None for x in got)
    text = fw.build_summary(got, SEND_AT)
    assert "Опціони проти RV30: розрахованих пар ще немає" in text and "Розраховано прогнозів: 32," in text


def test_only_open_forecasts(tmp_path):
    got, _ = fw.load_forecasts(_store(tmp_path / "o.sqlite", 23, _forecasts(0)))
    text = fw.build_summary(got, SAMPLE_NOW.timestamp())
    assert "Розрахованих прогнозів ще немає (відкритих: 16); перша оцінка — після delivery 30.10 (08:00 UTC)." in text


def test_untrusted_text_is_escaped_and_odd_rows_never_raise():
    text = fw.build_summary(None, SEND_AT, "unreadable: <b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "<b>x</b>" not in text
    odd = [{"kind": "<i>", "prob": "abc", "outcome": 1, "expiry_ts": 1, "status": "settled"}]
    assert "Підсумок не вдалося зібрати: несподіваний формат даних (ValueError)." in fw.build_summary(odd, SEND_AT)


def test_journal_button(tmp_path):
    url = tmp_path / "dashboard_url"
    assert fw.journal_buttons(url, environ={}, key_file=tmp_path / "nokey") == []
    url.write_text("http://tradedesk.example.ts.net/", encoding="utf-8")
    assert fw.journal_buttons(url, environ={}, key_file=tmp_path / "nokey") == []  # only https
    url.write_text("https://tradedesk.tail1234.ts.net\n", encoding="utf-8")
    assert fw.journal_buttons(url, environ={}, key_file=tmp_path / "nokey") == [("Журнал", "https://tradedesk.tail1234.ts.net/journal")]
    key = tmp_path / "dashboard.env"
    key.write_text("DASHBOARD_KEY=abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    assert fw.journal_buttons(url, environ={}, key_file=key) == [
        ("Журнал", "https://tradedesk.tail1234.ts.net/journal?k=abcdefghijklmnopqrstuvwxyz")
    ]


def test_main_preview_and_send(tmp_path, monkeypatch, capsys):
    path = _store(tmp_path / "t.sqlite", 23, _forecasts(3))
    monkeypatch.setattr(fw.time, "time", lambda: SEND_AT)

    def boom(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(tg_send, "send", boom)
    assert fw.main(["--db", str(path), "--print"]) == 0
    out = capsys.readouterr().out
    assert "Розраховано прогнозів: 48," in out and "[buttons: none (no $TRADEDESK_PANEL_URL" in out
    assert fw.main(["--db", str(path), "--out", str(tmp_path / "o")]) == 0
    assert "Журнал прогнозів" in (tmp_path / "o" / "forecast.html").read_text(encoding="utf-8")
    sent = []
    monkeypatch.setenv(fw.ab.PANEL_URL_ENV, "https://tradedesk.tail1234.ts.net/")
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert fw.main(["--db", str(path)]) == 0
    assert sent[0][1]["topic"] == "alerts" and sent[0][1]["buttons"] == [("Журнал", "https://tradedesk.tail1234.ts.net/journal")]
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or False)  # Telegram down
    assert fw.main(["--db", str(tmp_path / "none.sqlite")]) == 1 and "файла бази ще немає" in sent[-1][0]


def test_flag_path_matches_between_the_unit_and_step_26():
    unit = (ROOT / "deploy" / "user-units" / "tradedesk-forecast-weekly.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "user-units" / "tradedesk-forecast-weekly.timer").read_text(encoding="utf-8")
    step = (ROOT / "scripts" / "server" / "26-forecast-weekly-timer.sh").read_text(encoding="utf-8")
    cond = re.search(r"^ConditionPathExists=(\S+)$", unit, re.M).group(1)
    d = re.search(r"^DIR=(\S+)$", step, re.M).group(1)
    flag = re.search(r'^FLAG="\$DIR/(\S+)"$', step, re.M).group(1)
    assert cond == f"{d}/{flag}" and flag not in ("brief-telegram.on", "radar-telegram.on")  # its own switch
    assert "scripts/forecast_weekly_tg.py" in unit and re.search(r"^OnCalendar=Sat \*-\*-\* 12:00:00 Europe/Kyiv$", timer, re.M)
    assert "runuser" not in step and "sudo -" not in step and '"$(id -un)" == "$SVC"' in step  # runs as tradedesk
    assert re.search(r"^DB=(\S+)$", step, re.M).group(1) == "/var/lib/tradedesk-mcp/tradedesk.sqlite"  # dashboard.JOURNAL_DB on the server
