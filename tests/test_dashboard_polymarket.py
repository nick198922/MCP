"""scripts/dashboard.py, the «Polymarket» tab laid out by docs/design/PANEL.md (standing improvements, item 17): the
lead sentence, folded blocks with one-line summaries, short «i» notes, caveats once at the bottom, and the paper tests'
counters only (no-peeking rule). Real fixture rows in a temporary read-only store; no network."""

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

import test_dashboard as tdb  # noqa: E402

PM_B = tdb.PM_B
PM_A = 1792563645  # the fixture's earlier snapshot: nothing passes 5 pp, two sides at 3-5 pp
XSS = "<img src=x onerror=alert(1)>"


def _page(tmp_path: Path, now: float, rows: list[dict] | None = None) -> str:
    cols, fixture = tdb._pm_rows()
    p = tdb._pm_db(tmp_path / "s.sqlite", fixture if rows is None else rows, cols)
    data, err = db.load_pm_gap(p)
    assert err is None
    return db.render_polymarket(data, err, now, p)


def test_pm_tab_lead_folds_short_notes_and_caveats_at_the_bottom(tmp_path):
    page = _page(tmp_path, float(PM_B + 600))
    main = page[page.index("<main") : page.index("</main>")]
    # rule 1: the lead first, before any heading; both passing sides have R:R < 2 in the real snapshot
    lead = re.search(r"<div class='lead'>.*?</div>", main, re.S)
    assert lead and main.index("<div class='lead'>") < main.index("<h2>")
    assert "<span class='chip calm'>лише дорогі боки</span>" in lead.group(0)
    assert "Поріг 5 п.п. проходять боків: 2, але в усіх R:R нижче 2." in lead.group(0)
    assert "<b>Що робити:</b> нічого: за правилом 5 власника R:R має бути ≥ 2." in lead.group(0)
    # the passing sides stay open; the near table and the selection / paper test are folded with a summary line
    assert "<details" not in main[: main.index("<section class='wide fold'")]
    assert re.findall(r"<section class='wide fold' id='(.*?)'>", main) == ["pm-near", "pm-paper"] and "<details open" not in main
    assert "<span class='fs'>боків 0 · нижче порогу реєстрації, не сигнал</span>" in main
    summary = re.search(r"<span class='fs'>відбір пройшли (\d+) з (\d+) ринків · ([^<]*)</span>", main)
    assert summary and 0 < int(summary.group(1)) < int(summary.group(2))
    assert summary.group(3) == "паперовий тест v1: тиждень 1 з 16, знімків 4 · вердикт не раніше 23.02.2027"
    # rule 3: every «i» on the tab is the short instruction form, never the long «Для угоди» one
    for key in ("pm", "pm_near", "pm_model", "pm_edge", "pm_rr", "pm_paper"):
        assert key in db.ACT and db._info(key) in page, key
    assert "Для угоди:" not in page
    # rule 5: the caveats once, after the main content
    assert page.count("<b>Застереження</b>") == 1 and page.index("</main>") < page.index("<b>Застереження</b>")
    assert page.count("ризик-нейтральні") == 1


def test_pm_tab_paper_counters_only(tmp_path):
    cols, rows = tdb._pm_rows()
    base = tdb._pm_base(rows)
    # one snapshot before T0 v1 (not counted) and one after T0 v2-g (the latest, counted by both tests)
    extra = [{**base, "ts": db.PM_T0_V1 - 3600}, {**base, "ts": db.PM_T0_V2G + 10, "end_ts": db.PM_T0_V2G + 86_400}]
    p = tdb._pm_db(tmp_path / "s.sqlite", rows + extra, cols)
    data, err = db.load_pm_gap(p)
    assert err is None and data["ts"] == db.PM_T0_V2G + 10 and data["n_snaps"] == {"v1": 5, "v2g": 1}
    now = float(db.PM_T0_V2G + 600)
    page = db.render_polymarket(data, None, now, p)
    for text in (
        "<td>v1</td>",
        "<td>2 з 16</td>",  # 11.4 days after T0 v1
        "<td class='num'>09.02.2027</td>",  # v1 entry period ends T0 + 112 days
        "<td class='num'>23.02.2027</td>",
        "<td>v2-g</td>",
        "<td>1 з 16</td>",
        "<td class='num'>07.03.2027</td>",  # v2-g: T0 + 126 days, later than the v1 verdict
        f"<td class='num'>5 (≈ {int((now - db.PM_T0_V1) // 3600) + 1})</td>",
        "<td class='num'>1 (≈ 1)</td>",
    ):
        assert text in page, text
    # no-peeking: no result metric in the block, and the outcome table is never read
    fold = page[page.index("Відбір і паперовий тест") :]
    for word in ("Brier", "P&amp;L", "CLV", "виграш", "win rate"):
        assert word not in fold[: fold.index("</section>")], word


def test_pm_tab_never_reads_the_outcome_table(tmp_path, monkeypatch):
    cols, rows = tdb._pm_rows()
    p = tdb._pm_db(tmp_path / "s.sqlite", rows, cols)
    sql: list[str] = []
    real_connect = sqlite3.connect

    def spy(database, *a, **k):
        con = real_connect(database, *a, **k)
        con.set_trace_callback(sql.append)
        return con

    monkeypatch.setattr(db.sqlite3, "connect", spy)
    monkeypatch.setattr(db, "PM_DB", p)
    page = db.polymarket_page(now=float(PM_B + 600))
    assert "Відбір і паперовий тест" in page and sql
    assert not [s for s in sql if "pm_gap_outcome" in s]


def test_pm_paper_counts_dates_and_weeks():
    from tradedesk.analytics import pm_gap_report as R

    assert (db.PM_T0_V1, db.PM_T0_V2G, db.PM_MAIN_DAYS, db.PM_VERDICT_DAYS) == (
        R.T0_TS,
        R.T0_V2G_TS,
        R.MAIN_PERIOD_DAYS,
        R.VERDICT_DAYS,
    )
    v1, v2g = db.pm_paper_counts({"v1": 5, "v2g": "x"}, float(db.PM_T0_V1 - 1))
    assert (v1["week"], v1["expected"], v1["n"], v2g["n"]) == (0, 0, 5, None)
    assert db._pm_week(v1) == "ще не почався"
    v1, v2g = db.pm_paper_counts(None, float(db.PM_T0_V1 + 7 * 86_400))
    assert (v1["week"], v1["expected"], v1["n"]) == (2, 7 * 24 + 1, None)
    v1, v2g = db.pm_paper_counts({}, float(db.PM_T0_V1 + 112 * 86_400))
    assert v1["week"] is None and db._pm_week(v1) == "вхід закрито, чекаємо розв'язання" and v2g["week"] == 15
    assert v2g["verdict"] == db.PM_T0_V2G + 126 * 86_400 > v1["verdict"] == db.PM_T0_V1 + 126 * 86_400


def test_pm_lead_cases(tmp_path):
    cols, rows = tdb._pm_rows()
    side = {"edge": 0.071, "rr": 3.0, "side": "yes", "asset": XSS, "end_ts": PM_B + 5 * 3600}
    lead = db.pm_lead({"pass": [side, {**side, "rr": 1.2}], "near": []}, PM_B, float(PM_B + 600))
    assert "<span class='chip opp'>є розрив</span>" in lead and "проходять боків: 1; найбільший edge +7.1 п.п." in lead
    assert "до кінця 4.8 год" in lead and "journal_add" in lead and "не раніше 23.02.2027" in lead
    assert "<img" not in lead and "&lt;img" in lead
    # nothing passes: the near count, nothing to do
    page = _page(tmp_path, float(PM_A + 600), [r for r in rows if r["ts"] <= PM_A])
    assert "<span class='chip calm'>розривів немає</span>" in page
    assert "Жоден бік не проходить поріг 5 п.п.; близько (3–5 п.п.): 2." in page
    # a stale snapshot: the lead says so, the old rows stay collapsed and are not signals
    stale = db.render_polymarket(*db.load_pm_gap(tdb._pm_db(tmp_path / "t.sqlite", rows, cols)), float(PM_B + 2 * 3600 + 60))
    assert "<span class='chip risk'>ціни застарілі</span>" in stale and "Останній знімок 2 год тому" in stale
    assert "за цими цінами не входь" in stale and "Ціни застарілі (знімок старший за 2 год): це не сигнали" in stale


def test_pm_tab_empty_states(tmp_path):
    cols, _ = tdb._pm_rows()
    for data, err, text in (
        (*db.load_pm_gap(tdb._pm_db(tmp_path / "e.sqlite", [], cols)), "Знімків ще немає"),
        (*db.load_pm_gap(tmp_path / "none.sqlite"), "Бази ще немає"),
    ):
        page = db.render_polymarket(data, err, float(PM_B))
        assert text in page and "aria-current=page>Polymarket" in page and page.endswith("</body></html>")
        assert page.count("<b>Застереження</b>") == 1 and "class='lead'" not in page and "wide fold" not in page
