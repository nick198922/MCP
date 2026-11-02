"""scripts/dashboard.py, the «Опціони» tab laid out by docs/design/PANEL.md (standing improvements, item 18): the lead
(«what now / what to do»), a high-impact event within 48 h and the owner's open Bybit positions always visible, the
form, chart, tiles and rules open, the rest folded with one-line summaries, short «i» notes, the caveats once at the
bottom. Real fixtures (the 20.10 snapshot, the 26.10 live file shape); no network."""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

import test_dashboard as tdb  # noqa: E402
import test_dashboard_positions as tp  # noqa: E402

XSS = "<img src=x onerror=alert(1)>"
ICON = "<span class='info' tabindex='0' role='button'"
FOLDS = ("verdict", "proscons", "riskfit", "greeks", "optscen", "volcost")


def _lead(page: str) -> str:
    m = re.search(r"<div class='lead'>.*?</div>", page, re.S)
    assert m, "no lead"
    return m.group(0)


def _main(page: str) -> str:
    return page[page.index("<main") : page.index("</main>")]


def test_options_tab_lead_open_blocks_folds_and_caveats_at_the_bottom():
    snap, asof = tdb._snap()
    page = db.render_options(snap, None, asof + 300, db.parse_options_query(""))
    main = _main(page)
    # rule 1: the lead first, before any heading; 0.1 BTC of the 25Δ strangle breaks rules 1 and 8 ($291 > $100)
    lead = _lead(main)
    assert main.index("<div class='lead'>") < main.index("<h2>")
    assert "<span class='chip risk'>порушує правила</span>" in lead
    assert "Купівля стренглу 0.10 BTC на 27.11 (до неї 37.5 дн): платиш $291, макс. збиток $291, беззбиток $74.1k / $92.9k." in lead
    assert "<b>Що робити:</b> зменш розмір до 0.03 BTC — тоді вкладається в 1 % депозиту (правила 1 і 8)." in lead
    # open: the form, the chart, the position tiles and (rule 6) the owner's rules; the rest folded, none open
    first_fold = main.index("<section class='wide fold'")
    for text in ("id='opt-form'", "<svg", "<h2>Позиція" + ICON, "<h2>Правила власника перед угодою" + ICON):
        assert main.index(text) < first_fold, text
    assert "<details" not in main[:first_fold] and "<details open" not in main
    assert main.count("<section class='wide fold'") == len(FOLDS)
    for key in FOLDS:
        assert f"<section class='wide fold' id='o-{key}'>" in main, key
    summaries = re.findall(r"<span class='fs'>(.*?)</span>", main)
    assert summaries == [
        "купуєш волатильність · заробляєш нижче $74.1k або вище $92.9k на 27.11",
        "за 1 · проти 1 · обережно 1",
        "макс. збиток $291 — у 2.9 раза більше за ризик $100 · під ризик — до 0.03 BTC",
        "дельта +0.002 BTC · вега +$17 · тета −$8 за день",
        "від −$291 до +$548 на русі BTC до ±15 %",
        "ATM IV 27.11 35.8 · DVOL на 16-му перцентилі за рік · DVOL − RV30 +5.2 вп",
    ]
    # rule 2: every tile has an action line; the max-loss one says what size fits
    tiles = re.findall(r"<div class='tile[^']*'>.*?</div>", main, re.S)
    assert len(tiles) == 8 and all("<span class='act'>" in t for t in tiles)
    assert "<span class='act'>зменш розмір до 0.03 BTC</span>" in main
    # rule 3: every «i» on the tab is the short instruction form
    n = page.count(ICON)
    assert n >= 15 and page.count("<span class='do'><i>") == 2 * n and "Для угоди:" not in page
    # rule 5: the caveats once, after the main content; the risk-neutral and H1 notes are not repeated in the blocks
    assert page.count("<b>Застереження</b>") == 1 and page.index("</main>") < page.index("<b>Застереження</b>")
    assert page.count("ризик-нейтральн") == 1 and page.count("відхилено (H1)") == 1
    assert page.index("ризик-нейтральн") > page.index("</main>")
    # the opened folds survive the form's resubmit (sessionStorage), the page itself does not auto-refresh
    assert "td-opt-open" in page and "section.fold[id]" in page and db.REFRESH_MARK not in page


def test_options_lead_cases():
    snap, asof = tdb._snap()

    def lead(q: str, now: float) -> str:
        return _lead(db.render_options(snap, None, now, db.parse_options_query(q)))

    ok = lead("qty=0.03", asof + 300)
    assert "<span class='chip calm'>у межах правил 1 і 8</span>" in ok and "journal_add" in ok and "Подія" not in ok
    sale = lead("s=short_strangle", asof + 300)
    assert "макс. збиток необмежений" in sale and "додай захисні опціони (залізний кондор) або стоп" in sale
    condor = lead("s=iron_condor", asof + 300)
    assert "отримуєш $194" in condor and "зменш розмір до 0.01 BTC" in condor and "(правило 1)" in condor
    # rule 8 by the page's clock: under 24 h never buy; 1-3 days without a thesis; the time agrees with the rule row
    odte = lead("qty=0.01", datetime(2026, 11, 26, 20, tzinfo=UTC).timestamp())
    assert "(до неї 12 год)" in odte and "не купуй: до експірації менше доби (правило 8)" in odte
    late = lead("qty=0.01", datetime(2026, 11, 24, 8, tzinfo=UTC).timestamp())
    assert "(до неї 72 год)" in late and "впиши тезу й ціль у форму" in late
    # a crossed condor: explained, not raised
    bad = lead("cur=iron_condor&kputw=80000&kput=77000", asof + 300)
    assert "<span class='chip ev'>не зібрано</span>" in bad and "обери інші страйки" in bad


def test_options_lead_shows_a_high_impact_event_within_48h():
    snap, asof = tdb._snap()  # the calendar's next high-impact event: Oct 2026 FOMC, 28.10 18:00 UTC
    t = datetime(2026, 10, 27, 20, tzinfo=UTC).timestamp()  # 22 h before it
    assert db.opt_event_soon(snap, asof) is None and db.opt_event_soon(snap, t) == ("Oct 2026 FOMC", 22.0)
    assert db.opt_event_soon(snap, datetime(2026, 10, 28, 19, tzinfo=UTC).timestamp()) is None  # already past
    ok = _lead(db.render_options(snap, None, t, db.parse_options_query("qty=0.03")))
    assert "<span class='chip risk'>подія</span>" in ok and "Подія з високим впливом: Oct 2026 FOMC через 22 год." in ok
    assert "<b>Що робити:</b> до релізу" in ok
    # a broken rule keeps the lead's advice, the event still shows in «what now»
    big = _lead(db.render_options(snap, None, t, db.parse_options_query("")))
    assert "порушує правила" in big and "Oct 2026 FOMC через 22 год" in big and "зменш розмір" in big
    # hostile calendar text is escaped
    ev = json.loads(json.dumps(snap))
    for x in ev["sections"]["events"]["data"]["events"]:
        if x.get("kind") == "fomc":
            x["title"] = XSS
    page = db.render_options(ev, None, t, db.parse_options_query("qty=0.03"))
    assert "<img" not in page and "&lt;img src=x" in _lead(page)


def test_options_tab_shows_the_owners_open_bybit_positions():
    snap, _ = tdb._snap()
    page = db.render_options(snap, None, tp.NOW, db.parse_options_query(""), None, (tp._payload(), None))
    main = _main(page)
    line = re.search(r"<p class='note warn'>Відкрито на Bybit позицій: .*?</p>", main)
    assert line and main.index("<div class='lead'>") < line.start() < main.index("<h2>")
    assert "опціонних 1 (P&amp;L −$3; найближча експірація 13.11 08:00 UTC, до неї 17.5 дн)" in line.group(0)
    assert "<a href='/positions'>вкладка «Позиції»</a>" in line.group(0) and "застарілі" not in line.group(0)
    stale = db.opt_live_line((tp._payload(), None), tp.NOW + 3600)
    assert "Дані застарілі (1 год тому)" in stale
    assert db.opt_live_line((tp._payload(positions=[]), None), tp.NOW).startswith("<p class='note'>Відкритих позицій на Bybit немає.")
    assert "немає ключа Bybit" in db.opt_live_line((tp._payload(status="no_key"), None), tp.NOW)
    assert db.opt_live_line((None, "missing"), tp.NOW) == "" and db.opt_live_line(None, tp.NOW) == ""
    # no chain in the snapshot: the line still shows
    no_chain = json.loads(json.dumps(snap))
    no_chain["sections"].pop("chain")
    page = db.render_options(no_chain, None, tp.NOW, None, None, (tp._payload(), None))
    assert "Ланцюжка опціонів у знімку немає" in page and "Відкрито на Bybit позицій: опціонних 1" in page


def test_options_page_reads_the_live_file(tmp_path, monkeypatch):
    p = tmp_path / "live.json"
    p.write_text(json.dumps(tp._payload()), encoding="utf-8")
    monkeypatch.setattr(db, "SNAPSHOT_PATH", tdb.SNAP_FIXTURE)
    monkeypatch.setattr(db, "LIVE_PATH", p)
    assert "Відкрито на Bybit позицій: опціонних 1" in db.options_page("", now=tp.NOW)
    monkeypatch.setattr(db, "LIVE_PATH", tmp_path / "none.json")
    page = db.options_page("", now=tp.NOW)
    assert "Відкрито на Bybit" not in page and "class='lead'" in page


def test_options_tab_empty_states():
    snap, asof = tdb._snap()
    no_chain = json.loads(json.dumps(snap))
    no_chain["sections"].pop("chain")
    for page, text in (
        (db.render_options(None, "missing", asof), "Знімка асистента ще немає"),
        (db.render_options(None, "corrupt", asof), "пошкоджений"),
        (db.render_options(no_chain, None, asof + 300), "Ланцюжка опціонів у знімку немає"),
        (db.render_options({}, None, asof), "Ланцюжка опціонів у знімку немає"),
    ):
        assert text in page and "aria-current=page>Опціони" in page and page.endswith("</body></html>"), text
        assert page.count("<b>Застереження</b>") == 1 and "class='lead'" not in page and "wide fold" not in page
        if "</main>" in page:
            assert page.index("</main>") < page.index("<b>Застереження</b>")
