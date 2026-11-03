"""scripts/dashboard.py, the «Радар» tab laid out by docs/design/PANEL.md (standing improvements, item 20): the lead
(«what now / what to do») and a high-impact event within 48 h always visible, four tiles with action lines instead of
the old five, every block folded with a one-line summary, short «i» notes, the caveats once at the bottom. Every number
comes from analytics.radar.radar_view (the real 18.10 section, as tests/test_dashboard.py builds it); no network."""

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

import test_dashboard as tdb  # noqa: E402

ICON = "<span class='info' tabindex='0' role='button'"
FOLDS = ("now", "price", "term", "rank", "ivrv", "skew", "smile", "skewhist", "fwd", "deal")


def _lead(page: str) -> str:
    m = re.search(r"<div class='lead'>.*?</div>", page, re.S)
    assert m, "no lead"
    return m.group(0)


def _main(page: str) -> str:
    return page[page.index("<main") : page.index("</main>")]


def _page(snap: dict, t: float, asset: str = "BTC") -> str:
    return db.render_radar(snap, None, t, db.parse_radar_query(f"asset={asset}"))


def _tile_acts(page: str) -> tuple[str, str]:
    """The action lines of the IV Rank and IV − RV30 tiles (the 2nd and 3rd tile)."""
    acts = re.findall(r"<span class='act'>(.*?)</span>", _main(page))
    return acts[1], acts[2]


def test_radar_tab_lead_tiles_folds_and_caveats_at_the_bottom():
    snap, t = tdb._radar_snap()
    page = _page(snap, t + 300)
    main = _main(page)
    # rule 1: the lead first, before any heading; one sentence of radar_view's numbers and what to do
    lead = _lead(main)
    assert main.index("<div class='lead'>") < main.index("<h2>")
    assert "<span class='chip ev'>гарячий тиждень</span>" in lead
    assert (
        "BTC 81 093 (18.10, 19:00 UTC; −3.0 % за тиждень); IV 30 днів 37.0 % проти реалізованої 31.6 %; IV Rank 10.7 % "
        "(біля дна року); найдорожчий тиждень 30.10–06.11 (US midterm elections 2026 (election day))." in lead
    )
    assert "<b>Що робити:</b> опціон під подію — з експірацією після неї" in lead and "Подія з високим впливом" not in lead
    # rules 2 and 7: four tiles (instead of the old 3 price + 2 skew), each with an action line, before the first fold
    first_fold = main.index("<section class='wide fold'")
    tiles = re.findall(r"<div class='tile[^']*'>.*?</div>", main[:first_fold], re.S)
    assert len(tiles) == 4 and len(re.findall(r"<div class='tile(?:'| t-)", main)) == 4
    assert all("<span class='act'>" in x for x in tiles)
    acts = re.findall(r"<span class='act'>(.*?)</span>", main)
    assert acts == [
        "звичайний тиждень: дивись IV і скью",
        "дешево за роком, дорого за рухом",  # IV Rank 10.7 % but IV − RV30 +5.4 вп: the tile agrees with its neighbour
        "покупець переплачує за рух",
        "захист дорожчає: пут-спред, не пут",
    ]
    assert all(len(a.split()) <= 8 for a in acts)
    for key in ("r_spot", "r_rank", "r_ivrv", "put_skew"):
        assert f"aria-label='Пояснення: {db.HELP[key][0]}'" in main[:first_fold], key
    # rule 4: every block folded (none open), its id stable for the keep-open script, a one-line summary each
    assert "<details open" not in main and "<details" not in main[:first_fold]
    assert main.count("<section class='wide fold'") == len(FOLDS)
    for key in FOLDS:
        assert f"<section class='wide fold' id='r-{key}'>" in main, key
    summaries = re.findall(r"<span class='fs'>(.*?)</span>", main)
    assert summaries == [
        "рядків: 5 · увага: вола, скью, події",
        "81 093 · −3.0 % за тиждень (18.10 проти 11.10)",
        "30 днів 37.0 % (+1.6 вп за тиждень) · фронт висхідний: 7 днів 34.1 %",
        "IV Rank 10.7 % · перцентиль 31 % · тиждень тому 7.6 %",
        "IV 30 днів 37.0 % · RV30 31.6 % · премія +5.4 вп",
        "put skew 2.6 вп, RR −3.7 вп · за тиждень +0.5 вп і −1.4 вп",
        "10Δ P 45.8 % · ATM 37.0 % · 10Δ C 36.8 % (18.10)",
        "днів в архіві: 2 · графік — від 20",
        "гарячий тиждень 30.10–06.11: 42.1 % проти медіани 36.5 %",
        "опціони дешеві відносно року (IV Rank 10.7 %) · висновків: 5",
    ]
    # one IV Rank format on the screen: the lead, the tile, its fold and the deal fold (review 02.11, p. 4)
    assert "<span class='num'>10.7 %</span>" in main and "IV Rank 11 %" not in page
    # rule 3: every «i» on the tab is the short instruction form
    n = page.count(ICON)
    assert n >= 14 and page.count("<span class='do'><i>") == 2 * n and "Для угоди:" not in page
    # rule 5: the caveats once, after the main content; risk-neutral and H1 only there
    assert page.count("<b>Застереження</b>") == 1 and page.index("</main>") < page.index("<b>Застереження</b>")
    for once in ("ризик-нейтральн", "відхилено (H1)", "Не інвестиційна порада", "не доводить, що ринок закладає"):
        assert page.count(once) == 1 and page.index(once) > page.index("</main>"), once
    assert "Блок не вдалося" not in page and "немає даних" not in page and page.endswith("</body></html>")


def test_radar_lead_cases():
    snap, t = tdb._radar_snap()
    eth = _lead(_page(snap, t + 300, "ETH"))
    assert "<span class='chip risk'>інверсія фронту</span>" in eth and "фронт інвертований: 7 днів 52.0 % проти 30 днів 51.1 %" in eth
    assert "тижневі опціони дорогі: якщо купувати — довший строк." in eth
    a = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    for w in a["forward_vol"]["windows"]:
        w["hot"] = False  # no hot week: the next rule that applies is protection getting dearer (+0.5 вп put skew)
    lead = _lead(_page(snap, t + 300))
    assert "<span class='chip risk'>захист дорожчає</span>" in lead and "пут-спредом, а не голим путом" in lead
    assert "найдорожчий тиждень" not in lead
    a["skew"]["change"] = None  # no week-ago skew: IV Rank at the year's bottom
    lead = _lead(_page(snap, t + 300))
    assert a["iv_vs_rv"]["premium_rv30"] > db.RADAR_RICH_IV  # the real 18.10 BTC section: IV 37.0 % against RV30 31.6 %
    # low IV Rank but IV above RV30 (review 02.11, blocker 1): not «cheap», as the IV − RV30 tile says
    assert "<span class='chip calm'>дешево за роком, дорого за рухом</span>" in lead
    assert "IV вища за фактичний рух на +5.4 вп" in lead and "опціони дешеві" not in lead and "chip opp" not in lead
    tiles = _main(_page(snap, t + 300))
    assert "<span class='act'>покупець переплачує за рух</span>" in tiles
    # the IV Rank tile follows the lead (review 02.11, p. 5): no «cheap» next to «покупець переплачує за рух»
    assert _tile_acts(_page(snap, t + 300)) == ("дешево за роком, дорого за рухом", "покупець переплачує за рух")
    a["iv_vs_rv"]["premium_rv30"] = db.RADAR_RICH_IV  # the boundary: not above RADAR_RICH_IV, the IV Rank rule stands
    assert _tile_acts(_page(snap, t + 300)) == ("дешево, але це не сигнал", "звична премія продавцю")
    assert "<span class='chip opp'>опціони дешеві</span>" in _lead(_page(snap, t + 300))
    a["iv_vs_rv"]["premium_rv30"] = 0.01  # IV barely above RV30: the IV Rank rule stands
    lead = _lead(_page(snap, t + 300))
    assert "<span class='chip opp'>опціони дешеві</span>" in lead and "лише під власну тезу й спредом" in lead
    assert _tile_acts(_page(snap, t + 300)) == ("дешево, але це не сигнал", "звична премія продавцю")
    a["iv_rank"]["now"]["rank"] = 0.7
    lead = _lead(_page(snap, t + 300))
    assert "<span class='chip risk'>опціони дорогі</span>" in lead and "IV Rank 70.0 % (біля верху року)" in lead
    assert _tile_acts(_page(snap, t + 300)) == ("дорого: продаж лише із захистом", "звична премія продавцю")
    a["iv_vs_rv"]["premium_rv30"] = -0.02  # high IV Rank but IV ≤ RV30: not «dear», as the IV − RV30 tile says
    page = _page(snap, t + 300)
    lead = _lead(page)
    assert "<span class='chip calm'>дорого за роком, не за рухом</span>" in lead and "опціони дорогі" not in lead
    assert "IV не вища за фактичний рух (−2.0 вп)" in lead
    assert "<span class='act'>опціони не дорожчі за рух</span>" in _main(page)
    assert _tile_acts(page) == ("дорого за роком, не за рухом", "опціони не дорожчі за рух")
    a["iv_vs_rv"]["premium_rv30"] = 0.0  # the boundary: IV equal to RV30 is «не дорожчі за рух» on the tile too
    page = _page(snap, t + 300)
    assert "дорого за роком, не за рухом" in _lead(page)
    assert _tile_acts(page) == ("дорого за роком, не за рухом", "опціони не дорожчі за рух")
    a["iv_vs_rv"]["premium_rv30"] = None  # no IV − RV30: the IV Rank rule alone
    page = _page(snap, t + 300)
    assert "<span class='chip risk'>опціони дорогі</span>" in _lead(page)
    acts = re.findall(r"<span class='act'>(.*?)</span>", _main(page))
    assert "дорого: продаж лише із захистом" in acts and len(acts) == 3  # no IV − RV30 tile, the rank's own line
    a["iv_rank"]["now"]["rank"] = 0.1
    assert "<span class='chip opp'>опціони дешеві</span>" in _lead(_page(snap, t + 300))
    assert "<span class='act'>дешево, але це не сигнал</span>" in _main(_page(snap, t + 300))
    a["iv_rank"]["now"]["rank"] = 0.4
    lead = _lead(_page(snap, t + 300))
    assert "<span class='chip calm'>спокій</span>" in lead and "нічого термінового" in lead and "(посередині року)" in lead
    assert db.radar_skew_move({"skew": {"change": {"put_skew_25": -0.005, "rr_25": None}}}) == "down"
    assert db.radar_skew_move({"skew": {"change": {"put_skew_25": 0.001, "rr_25": 0.002}}}) == "flat"
    assert db.radar_skew_move({"skew": {"change": {"put_skew_25": 0.0, "rr_25": -0.009}}}) == "up"


def test_radar_situation_uses_the_named_thresholds(monkeypatch):
    # review 02.11, p. 2: «Поточна ситуація» (and the Sunday Telegram radar) reads the same constants as the lead
    snap, _ = tdb._radar_snap()
    eth = snap["sections"]["radar"]["data"]["assets"]["ETH"]
    btc = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    vol = next(t for _, lab, t in db.radar_situation(eth, "ETH") if lab == "вола")
    skew = next((c, t) for c, lab, t in db.radar_situation(btc, "BTC") if lab == "скью")
    assert "інверсія фронту" in vol and skew[0] == "risk" and "подорожчав" in skew[1]
    monkeypatch.setattr(db, "RADAR_FRONT", 0.05)
    monkeypatch.setattr(db, "RADAR_FEAR_PS", 0.05)
    monkeypatch.setattr(db, "RADAR_FEAR_RR", 0.05)
    vol = next(t for _, lab, t in db.radar_situation(eth, "ETH") if lab == "вола")
    skew = next((c, t) for c, lab, t in db.radar_situation(btc, "BTC") if lab == "скью")
    assert "фронт плаский" in vol and skew[0] == "calm" and "майже не змінився" in skew[1]


def test_radar_lead_shows_a_high_impact_event_within_48h():
    # rule 6: the calendar's next high-impact event (Oct 2026 FOMC, 28.10 18:00 UTC) by the page's clock, as «Опціони»
    snap, _ = tdb._radar_snap()
    t = datetime(2026, 10, 27, 20, tzinfo=UTC).timestamp()  # 22 h before it
    lead = _lead(_page(snap, t))
    assert "<span class='chip risk'>подія</span>" in lead and "Подія з високим впливом: Oct 2026 FOMC через 22 год." in lead
    assert "<b>Що робити:</b> до релізу" in lead
    # an empty or missing archive still shows the event
    empty, _ = tdb._radar_snap(keep=set())
    a = empty["sections"]["radar"]["data"]["assets"]["BTC"]
    for k in list(a):
        a[k] = None
    lead = _lead(_page(empty, t))
    assert "<span class='chip ev'>бракує даних</span>" in lead and "Oct 2026 FOMC через 22 год" in lead
    no_sec, _ = tdb._snap()  # the 20.10 fixture predates the radar section
    lead = _lead(_page(no_sec, t))
    assert "Секції «радар» у знімку ще немає" in lead and "Oct 2026 FOMC через 22 год" in lead
    assert _lead(_page(no_sec, datetime(2026, 10, 28, 19, tzinfo=UTC).timestamp())).count("Подія") == 0  # already past


def test_radar_tab_empty_snapshot_and_empty_archive():
    # no snapshot file: the reason in the header, the caveats at the bottom, no lead
    page = db.render_radar(None, "missing", 0.0)
    assert "Знімка асистента ще немає" in page and "<main" not in page and page.endswith("</ul></div></body></html>")
    assert page.index("aria-current=page>Радар") < page.index("<b>Застереження</b>")
    # the archive job is off: the lead from the DVOL-based fields, IV Rank tile only, folds still with their titles
    empty, t = tdb._radar_snap(keep=set())
    page = _page(empty, t + 60)
    main = _main(page)
    lead = _lead(main)
    assert "<span class='chip opp'>опціони дешеві</span>" in lead and "IV Rank 10.7 % (біля дна року)." in lead
    assert len(re.findall(r"<div class='tile(?:'| t-)", main)) == 1 and "IV Rank" in main
    assert "Архіву поверхні BTC ще немає" in main and main.count("<section class='wide fold'") == len(FOLDS)
    summaries = re.findall(r"<span class='fs'>(.*?)</span>", main)
    assert summaries[0] == "рядків: 1 · без тривожних" and summaries[7] == "історії ще немає" and summaries[1] == ""
    # every field null: an explained lead, no tiles, no failed block
    a = empty["sections"]["radar"]["data"]["assets"]["BTC"]
    for k in list(a):
        a[k] = None
    page = _page(empty, t + 60)
    assert "<span class='chip ev'>бракує даних</span>" in page and "Даних радару BTC у знімку немає" in page
    assert "<div class='tile'" not in page and "Блок не вдалося" not in page and page.endswith("</body></html>")


def test_radar_tab_one_asset_only():
    snap, t = tdb._radar_snap()
    sec = snap["sections"]["radar"]
    del sec["data"]["assets"]["ETH"]
    sec["missing"] = [{"field": "radar:archive:ETH", "provider": "tradedesk", "reason": "error", "detail": "eth <b>down</b>"}]
    eth = _page(snap, t + 300, "ETH")
    lead = _lead(eth)
    assert "<span class='chip ev'>немає даних</span>" in lead and "Даних ETH у секції радару немає." in lead
    assert "eth &lt;b&gt;down&lt;/b&gt;" in eth and "<b>down" not in eth and "<details" not in _main(eth)
    assert eth.index("</main>") < eth.index("<b>Застереження</b>") and eth.count("<b>Застереження</b>") == 1
    btc = _page(snap, t + 300, "BTC")
    assert "<span class='chip ev'>гарячий тиждень</span>" in _lead(btc) and _main(btc).count("<section class='wide fold'") == len(FOLDS)
    assert "eth &lt;b&gt;down" not in btc  # the other asset's failure is not shown here


def test_radar_lead_escapes_snapshot_text():
    snap, t = tdb._radar_snap()
    a = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    hot = next(w for w in a["forward_vol"]["windows"] if w["hot"])
    hot["events"][0]["title"] = "<img src=x onerror=alert(1)>"
    a["surface"]["now_day"] = "<b>d</b>"
    lead = _lead(_page(snap, t + 300))
    assert "&lt;img src=x onerror=alert(1)&gt;" in lead and "<img" not in lead
    page = _page(snap, t + 300)
    assert "<img" not in page and "<b>d" not in page
