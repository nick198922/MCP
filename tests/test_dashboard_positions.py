"""«Позиції» tab: live Bybit positions, P&L, greeks and breakevens (scripts/dashboard.py) and its collector
(scripts/bybit_live.py). Network-free: the collector runs against a fake ccxt client."""

import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bybit_live as bl  # noqa: E402
import dashboard as db  # noqa: E402

NOW = datetime(2026, 10, 26, 19, 30, tzinfo=UTC).timestamp()
EXP = datetime(2026, 11, 13, 8, tzinfo=UTC).timestamp()


def _payload(**over) -> dict:
    """Shaped like a real live_positions.json (26.10.2026): a long 78P / 83C strangle on 13NOV26."""
    d = {
        "schema": 1,
        "source": "bybit",
        "asof_utc": "2026-10-26T19:29:55Z",
        "status": "ok",
        "positions": [
            {
                "symbol": "BTC-13NOV26-78000-P-USDT",
                "category": "option",
                "side": "Buy",
                "size": "0.1",
                "avgPrice": "1590",
                "markPrice": "1651",
                "unrealisedPnl": "6.1",
                "curRealisedPnl": "-1.60607719",
            },
            {
                "symbol": "BTC-13NOV26-83000-C-USDT",
                "category": "option",
                "side": "Buy",
                "size": "0.1",
                "avgPrice": "1520",
                "markPrice": "1459",
                "unrealisedPnl": "-6.1",
                "curRealisedPnl": "-1.60521736",
            },
        ],  # fmt: skip
        "tickers": {
            "BTC-13NOV26-78000-P-USDT": {
                "markPrice": "1653.88",
                "markIv": "0.3821",
                "underlyingPrice": "80289.6",
                "bid1Price": "1655",
                "ask1Price": "1660",
            },
            "BTC-13NOV26-83000-C-USDT": {
                "markPrice": "1458.55",
                "markIv": "0.3633",
                "underlyingPrice": "80289.6",
                "bid1Price": "1455",
                "ask1Price": "1460",
            },
        },
        "wallet": {"totalEquity": "1417.62", "totalAvailableBalance": "461.60"},
        "rv": {"BTC": {"rv7": 0.27, "rv30": 0.244}},
        "errors": [],
    }
    d.update(over)
    return d


def _calls_and_strangle() -> dict:
    """The account on 26.10: 84C and 86C (30OCT26) bought a day apart, plus the strangle whose legs are 19 min apart."""
    d = _payload()
    d["positions"][0]["openTime"] = 1793006243022  # 78P 09:17
    d["positions"][1]["openTime"] = "1793005121350"  # 83C 08:58 (a string, as Bybit sends createdTime)
    d["positions"] += [
        {"symbol": "BTC-30OCT26-86000-C-USDT", "category": "option", "side": "Buy", "size": "0.7", "avgPrice": "528.86",
         "curRealisedPnl": "-11.5", "openTime": 1791381486625},
        {"symbol": "BTC-30OCT26-84000-C-USDT", "category": "option", "side": "Buy", "size": "0.25", "avgPrice": "1069",
         "curRealisedPnl": "-4.08", "openTime": 1791477003361},
    ]  # fmt: skip
    d["tickers"]["BTC-30OCT26-86000-C-USDT"] = {"markPrice": "41.4", "markIv": "0.396", "underlyingPrice": "80129.2"}
    d["tickers"]["BTC-30OCT26-84000-C-USDT"] = {"markPrice": "125.1", "markIv": "0.367", "underlyingPrice": "80129.2"}
    return d


def test_separate_trades_get_separate_breakevens_and_a_strangle_stays_whole():
    groups, _ = db.live_groups(_calls_and_strangle(), NOW)
    assert [g["title"] for g in groups] == ["84 000 CALL", "86 000 CALL", "78 000 PUT + 83 000 CALL"]
    c84 = groups[0]
    (be,) = db.live_breakevens(c84, 0.0)
    assert be == pytest.approx(84000 + (1069 * 0.25 + 4.08 + 4.08) / 0.25, abs=1.0)  # strike + premium + fees in and out
    joined, _ = db.live_groups(_calls_and_strangle(), NOW, join=True)
    assert [g["title"] for g in joined] == ["84 000 CALL + 86 000 CALL", "78 000 PUT + 83 000 CALL"]


def test_page_toggles_between_separate_and_joined():
    sep = db.render_live_main(_calls_and_strangle(), None, NOW)
    assert "BTC 84 000 CALL · експірація" in sep and "href='/positions?join=1'" in sep
    joined = db.render_live_main(_calls_and_strangle(), None, NOW, join=True)
    assert "BTC 84 000 CALL + 86 000 CALL · експірація" in joined and "href='/positions'>окремо" in joined


def test_groups_use_live_marks_and_keep_fees():
    groups, linear = db.live_groups(_payload(), NOW)
    assert linear == [] and len(groups) == 1
    g = groups[0]
    assert g["base"] == "BTC" and g["exp_ts"] == EXP and g["F"] == pytest.approx(80289.6)
    assert g["days"] == pytest.approx((EXP - NOW) / 86400)
    assert g["upnl"] == pytest.approx(0.1 * (1653.88 - 1590) + 0.1 * (1458.55 - 1520))
    assert g["realised"] == pytest.approx(-3.21129455)
    assert g["rv7"] == 0.27


def test_breakeven_at_expiry_is_strike_minus_premium_and_fees():
    g = db.live_groups(_payload(), NOW)[0][0]
    lo, hi = db.live_breakevens(g, 0.0)
    fees = 3.21129455  # paid; exit fee at expiry for the leg that expires worthless is capped at 0
    prem = 159 + 152
    # below the put strike only the put pays: 0.1 × (78000 − S) = premium + entry fees + the put's delivery fee
    assert lo == pytest.approx(78000 - (prem + fees + 1.60607719) / 0.1, abs=0.5)
    assert hi == pytest.approx(83000 + (prem + fees + 1.60521736) / 0.1, abs=0.5)


def test_breakevens_widen_with_time_for_a_long_strangle():
    g = db.live_groups(_payload(), NOW)[0][0]
    now_lo, now_hi = db.live_breakevens(g, g["days"])
    exp_lo, exp_hi = db.live_breakevens(g, 0.0)
    assert exp_lo < now_lo < g["F"] < now_hi < exp_hi


def test_pnl_now_matches_marks_less_fees():
    g = db.live_groups(_payload(), NOW)[0][0]
    # the model at mark IV reprices Bybit's marks closely; fees: realised (entry) and the exit estimate
    model = db.live_pnl(g, g["F"], g["days"])
    assert model == pytest.approx(g["upnl"] + g["realised"] - 3.21, abs=2.0)


def test_greeks_and_theta_gamma_move():
    g = db.live_groups(_payload(), NOW)[0][0]
    G = db.live_greeks(g)
    assert G["theta"] < 0 < G["gamma"] and G["vega"] > 0
    # Black-76 identity: the theta/gamma breakeven move is the IV's implied daily move
    assert G["tg_move"] == pytest.approx(G["move_1d"], rel=0.05)


def test_page_renders_tiles_tables_chart_and_realised_move():
    page = db.render_positions(_payload(), None, NOW)
    for s in ("Позиції Bybit", "id='live'", "Беззбиток", "Греки", "P&amp;L від ціни", "<svg", "Сценарії", "реальний рух BTC", "$1 418"):
        assert s in page, s
    assert "href='/positions' aria-current=page" in page
    assert "дані застарілі" not in page and "оновлюється кожні 10 с" in page


def test_fragment_is_the_live_part_only(tmp_path, monkeypatch):
    p = tmp_path / "live.json"
    p.write_text(json.dumps(_payload()))
    monkeypatch.setattr(db, "LIVE_PATH", p)
    frag = db.positions_page("frag=1", now=NOW)
    assert "<html" not in frag and "Беззбиток" in frag
    assert "<html" in db.positions_page("", now=NOW)


def test_stale_missing_and_status_states(tmp_path):
    assert "дані застарілі" in db.render_live_main(_payload(), None, NOW + 600)
    assert "Живих даних ще нема" in db.render_live_main(*db.load_live(tmp_path / "none.json"), NOW)
    assert "не лише для читання" in db.render_live_main(_payload(status="key_not_read_only", positions=[]), None, NOW)
    assert "Відкритих позицій немає" in db.render_live_main(_payload(positions=[]), None, NOW)


def test_untrusted_and_malformed_rows():
    bad = _payload()
    bad["positions"] += [
        {"symbol": "<script>x</script>", "category": "linear", "side": "Sell", "size": "2", "avgPrice": "10", "liqPrice": "15"},
        {"symbol": "BTC-13NOV26-90000-C-USDT", "category": "option", "side": "Buy", "size": "abc", "avgPrice": "1"},
        "junk",
    ]
    page = db.render_live_main(bad, None, NOW)
    assert "<script>x" not in page and "&lt;script&gt;" in page
    assert "Ф’ючерси" in page and "90 000" not in page


@pytest.mark.parametrize(("closes", "days", "want"), [([100.0] * 31, 30, 0.0), ([100.0, 110.0], 7, None)])
def test_realised_vol(closes, days, want):
    assert bl.realised_vol(closes, days) == want


def test_realised_vol_alternating():
    closes = [100.0, 110.0] * 4  # 7 returns of ±log(1.1)
    assert bl.realised_vol(closes, 7) == pytest.approx(math.log(1.1) * math.sqrt(365) * math.sqrt(1 - 1 / 49), rel=1e-6)


class FakeEx:
    def __init__(self, read_only="1", fail_positions=False):
        self.read_only, self.fail_positions, self.calls = read_only, fail_positions, []

    def privateGetV5UserQueryApi(self):  # noqa: N802
        return {"result": {"readOnly": self.read_only}}

    def privateGetV5PositionList(self, p):  # noqa: N802
        self.calls.append(("pos", p.get("category")))
        if self.fail_positions:
            raise RuntimeError("boom secret=abc")
        rows = _payload()["positions"] if p.get("baseCoin") == "BTC" else []
        return {"result": {"list": [dict(r, theta="-7", gamma="0.0000055") for r in rows]}}

    def privateGetV5AccountWalletBalance(self, p):  # noqa: N802
        return {"result": {"list": [{"totalEquity": "1417.6", "apiKey": "never-copied"}]}}

    def publicGetV5MarketTickers(self, p):  # noqa: N802
        self.calls.append(("tick", p.get("baseCoin")))
        return {"result": {"list": [dict(v, symbol=k) for k, v in _payload()["tickers"].items()]}}

    def publicGetV5MarketKline(self, p):  # noqa: N802
        rows = [[str(1790000000000 + i * 86400000), "0", "0", "0", str(80000 + (i % 2) * 800)] for i in range(32)]
        return {"result": {"list": list(reversed(rows))}}


def test_collector_one_pass_writes_fixed_fields(tmp_path):
    out = tmp_path / "live.json"
    assert bl.run(FakeEx(), out, 0, 10, 30) == "ok"
    d = json.loads(out.read_text())
    assert d["status"] == "ok" and len(d["positions"]) == 2 and set(d["tickers"]) == {p["symbol"] for p in d["positions"]}
    assert d["wallet"] == {k: ("1417.6" if k == "totalEquity" else None) for k in bl.WALLET_FIELDS}
    assert d["rv"]["BTC"]["rv7"] > 0 and d["positions"][0]["category"] == "option"
    assert "never-copied" not in out.read_text()


def test_collector_refuses_a_trading_key_and_hides_errors(tmp_path):
    out = tmp_path / "live.json"
    assert bl.run(FakeEx(read_only="0"), out, 0, 10, 30) == "key_not_read_only"
    assert bl.run(FakeEx(fail_positions=True), out, 0, 10, 30) == "error"
    text = out.read_text()
    assert "secret" not in text and json.loads(text)["status"] == "error"


def test_collector_loop_refreshes_tickers_often_and_positions_less(tmp_path):
    t = [0.0]
    ex = FakeEx()
    bl.run(ex, tmp_path / "live.json", 55, 10, 30, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    ticks = sum(1 for c in ex.calls if c[0] == "tick")
    pos_rounds = sum(1 for c in ex.calls if c == ("pos", "option")) // 5  # 5 option calls per round
    assert ticks == 6 and pos_rounds == 2


def test_dashboard_has_no_duplicate_top_level_names():
    """A second ``def`` with the same name silently replaces the first (a helper of the brief once broke this way)."""
    import ast

    tree = ast.parse((Path(db.__file__)).read_text(encoding="utf-8"))
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert sorted({n for n in names if names.count(n) > 1}) == []


def test_breakeven_rows_are_dated_daily_then_weekly():
    g = db.live_groups(_payload(), NOW)[0][0]  # 26.10 19:30 UTC, expiry 13.11 08:00 UTC
    ts = db.live_be_dates(g)
    days = [datetime.fromtimestamp(t, UTC).strftime("%d.%m %H") for t in ts]
    assert days[:7] == ["27.10 08", "28.10 08", "29.10 08", "30.10 08", "31.10 08", "01.11 08", "02.11 08"]
    assert days[7:] == ["09.11 08", "13.11 08"] and ts[-1] == EXP
    html = db.block_live_be(g)
    assert "лишиться" not in html and "вт 27.10, 10:00" in html and "пт 13.11, 10:00" in html  # Kyiv, winter time
    rows = html.count("<tr>") - 1
    assert rows == 1 + len(ts)


def test_a_long_call_has_no_lower_breakeven_column():
    c84 = db.live_groups(_calls_and_strangle(), NOW)[0][0]
    html = db.block_live_be(c84)
    assert "беззбиток вище" in html and "беззбиток нижче" not in html
    strangle = db.block_live_be(db.live_groups(_calls_and_strangle(), NOW)[0][2])
    assert "беззбиток нижче" in strangle and "беззбиток вище" in strangle


# ---------------------------------------------------------------- the tab laid out by docs/design/PANEL.md (item 19)

ICON = "<span class='info' tabindex='0' role='button'"


def _lead(page: str) -> str:
    m = re.search(r"<div class='lead'>.*?</div>", page, re.S)
    assert m, "no lead"
    return m.group(0)


def _main(page: str) -> str:
    return page[page.index("<main") : page.index("</main>")]


def _with_short(liq: str = "3400") -> dict:
    """The 26.10 strangle plus a short ETH perp whose liquidation is 9.7 % above its mark."""
    d = _payload()
    d["positions"].append(
        {"symbol": "ETHUSDT", "category": "linear", "side": "Sell", "size": "0.5", "avgPrice": "3000", "markPrice": "3100",
         "liqPrice": liq, "leverage": "10", "curRealisedPnl": "-0.5", "openTime": 1793000000000}
    )  # fmt: skip
    return d


def test_positions_tab_lead_open_table_folds_and_caveats_at_the_bottom():
    page = db.with_help(db.render_positions(_calls_and_strangle(), None, NOW))
    main = _main(page)
    # rule 1: the lead first, before any heading: count, total P&L, the nearest bound; the advice
    lead = _lead(main)
    assert main.index("<div class='lead'>") < main.index("<h2>")
    assert (
        "Відкрито позицій: 3 (опціонних 3), P&amp;L −$596 (−42.0 % капіталу); найближча межа — експірація BTC 30.10 08:00 UTC "
        "(через 3.5 дн)." in lead
    )
    # an open loss above one trade's risk budget: a fact against the owner's deposit ($10 000, rule 1), no new rule
    assert "<span class='chip risk'>збиток понад $100</span>" in lead
    assert (
        "звір кожну позицію з планом виходу: відкритий збиток $596 — 6.0 % депозиту $10 000, більше за ризик на одну угоду "
        "за правилом 1 ($100)." in lead
    )
    assert "нових угод не відкривай" not in page and "великий збиток" not in page
    # rule 2: every tile has an action line; the risk per trade comes from the rules' deposit, as on «Опціони»
    tiles = re.findall(r"<div class='tile[^']*'>.*?</div>", main, re.S)
    assert len(tiles) == 4 and all("<span class='act'>" in t for t in tiles)
    assert "<span class='act'>ризик на угоду $100 (правило 1)</span>" in main and "≈ $14" not in main
    assert "реалізовано −$" in main and "Ризик на угоду — 1 % депозиту ($100, правило 1)" in page
    # rule 6 / 8: the open positions always visible, one row each with the side label, before every fold
    first_fold = main.index("<section class='wide fold'")
    table = main[main.index("<h2>Відкриті позиції" + ICON) : first_fold]
    assert table.count("<span class='badge long'>лонг</span>") == 3 and "<details" not in main[:first_fold]
    assert "<th class='num'>Реалізовано</th>" in table and "Комісії" not in table
    assert "$267 → $31" in table and "−89.82 %" in table and "0.10 BTC × 2 ноги" in table and "експірація 30.10 08:00 UTC" in table
    # rule 4: each position's details folded, none open, the breakevens and theta in the one-line summary
    assert "<details open" not in main and main.count("<section class='wide fold'") == 3
    assert re.findall(r"<section class='wide fold' id='(.*?)'>", main) == [
        "lp-BTC-1793347200-84000c",
        "lp-BTC-1793347200-86000c",
        "lp-BTC-1794556800-78000p-83000c",
    ]
    summaries = re.findall(r"<span class='fs'>(.*?)</span>", main)
    assert len(summaries) == 3 and all(s.startswith("беззбиток зараз $") and "/добу · дельта" in s for s in summaries)
    assert "$79.2k / $81.3k" in summaries[2]  # a strangle: both breakevens
    for text in ("Беззбиток", "Греки", "P&amp;L від ціни", "Сценарії"):
        assert main.index(text) > first_fold, text
    # rule 3: every «i» on the tab is the short instruction form
    n = page.count(ICON)
    assert n >= 15 and page.count("<span class='do'><i>") == 2 * n and "Для угоди:" not in page
    # rule 5: the caveats once, after the main content; the model is not repeated in the blocks
    assert page.count("<b>Застереження</b>") == 1 and page.index("</main>") < page.index("<b>Застереження</b>")
    assert page.count("ризик-нейтральн") == 1 and page.index("ризик-нейтральн") > page.index("</main>")
    assert "Black-76, кожна нога" not in main and "Стоп-ордерів збирач не читає" in page[page.index("</main>") :]
    # the 10-second refresh keeps the opened folds open (by section id); the fold ids do not depend on the clock
    assert "section.fold[id] > details" in page and "open[d.parentNode.id]" in page
    later = db.render_live_main(_calls_and_strangle(), None, NOW + 10)
    assert re.findall(r"fold' id='(.*?)'", later) == re.findall(r"fold' id='(.*?)'", main)


def test_positions_lead_cases():
    def lead(data: dict, now: float = NOW, **kw) -> str:
        return _lead(db.render_live_main(data, None, now, **kw))

    calm = lead(_payload())
    assert "<span class='chip calm'>нічого термінового</span>" in calm
    assert "Відкрито позицій: 1 (опціонних 1), P&amp;L −$3 (−0.2 % капіталу)" in calm and "до експірації 13.11 08:00 UTC" in calm
    # a macro event within 48 h joins «what now» (rule 6) and is the advice when nothing more urgent applies
    ev = [{"ts": NOW + 20 * 3600, "label": "ФРС: рішення щодо ставки"}, {"ts": NOW + 72 * 3600, "label": "CPI США (інфляція)"}]
    soon = lead(_payload(), events=ev)
    assert "<span class='chip ev'>подія</span>" in soon and "Подія з високим впливом: ФРС: рішення щодо ставки через 20 год." in soon
    assert "CPI" not in soon and "Подія" not in lead(_payload(), events=ev[1:])
    # a liquidation within 15 % of the mark comes first, the event still shows
    liq = lead(_with_short(), events=ev)
    assert "<span class='chip risk'>ліквідація близько</span>" in liq and "ліквідація ETHUSDT 3 400.00 (9.7 % від марки)" in liq
    assert "ETHUSDT: постав стоп на Bybit перед ліквідацією" in liq and "ФРС" in liq and "ф&#x27;ючерсних 1" in liq
    assert "ліквідація близько" not in lead(_with_short("4000"))  # 29 % away
    # under a day to the nearest expiry
    t = EXP - 12 * 3600 - 300
    late = lead(_payload(asof_utc=datetime.fromtimestamp(t - 5, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")), t)
    assert "<span class='chip risk'>експірація &lt; доби</span>" in late and "(через 12 год)" in late
    assert "виріши до 13.11 08:00 UTC: закрити BTC 78 000 PUT + 83 000 CALL чи дати експірувати" in late
    # nothing open
    empty = db.render_live_main(_payload(positions=[]), None, NOW)
    assert "<span class='chip calm'>позицій немає</span>" in _lead(empty) and "Відкритих позицій на Bybit немає." in _lead(empty)
    assert "Відкритих позицій немає." in empty and "wide fold" not in empty and "окремо кожна угода" not in empty
    # stale data: whatever else applies, do not decide on these numbers
    stale = lead(_with_short(), NOW + 600)
    assert "<span class='chip risk'>дані застарілі</span>" in stale and "Дані 10 хв тому" in stale and "tradedesk-bybit-live" in stale


def test_positions_open_table_futures_row_and_fold():
    page = db.render_live_main(_with_short(), None, NOW)
    row = re.search(r"<tr><td><b>ETHUSDT</b>.*?</tr>", page).group(0)
    assert "<span class='badge short'>шорт</span>" in row and "3 000.00 → 3 100.00" in row and "−3.37 %" in row and "−$50" in row
    assert "<td class='bad'>ліквідація 3 400.00<span class='sub'>9.7 % від марки</span>" in row and "11 год" in row
    assert "<section class='wide fold' id='lp-linear'>" in page
    assert "позицій 1 · найближча ліквідація ETHUSDT за 9.7 % від марки · плече до 10.0×" in page
    no_liq = db.render_live_main(_with_short(""), None, NOW)
    assert "ліквідації немає" in no_liq and "найближча ліквідація" not in no_liq


def test_positions_past_expiry_awaits_settlement_without_advice():
    """An option past its expiry that Bybit has not settled yet: no «decide by <a past time>», no «under a day»."""
    t = EXP + 3600
    data = _payload(asof_utc=datetime.fromtimestamp(t - 5, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    main = db.render_live_main(data, None, t)
    lead = _lead(main)
    assert "<span class='chip ev'>експірація минула</span>" in lead and "виріши до" not in lead and "доби" not in lead
    assert "Експірація минула, чекає розрахунку Bybit: BTC 78 000 PUT + 83 000 CALL (13.11 08:00 UTC)." in lead
    assert "дочекайся розрахунку Bybit" in lead and "найближча межа" not in lead
    assert "<td>експірація 13.11 08:00 UTC<span class='sub'>минула, чекає розрахунку Bybit</span>" in main
    assert "<span class='act'>чекає розрахунку Bybit</span>" in main and "<span class='act'>вирішуй" not in main
    # a live position beside it keeps its own advice
    with_short = _with_short()
    with_short["asof_utc"] = data["asof_utc"]
    both = _lead(db.render_live_main(with_short, None, t))
    assert "ліквідація близько" in both and "Експірація минула" in both


def test_position_fold_ids_keep_fractional_strikes_apart():
    def g(*strikes: float) -> dict:
        return {"base": "SOL", "exp_ts": EXP, "legs": [{"strike": s, "kind": "call"} for s in strikes]}

    ids = [db._live_fold_id(g(s)) for s in (152.5, 152.0, 152.25)]
    assert ids == [f"lp-SOL-{int(EXP)}-152_5c", f"lp-SOL-{int(EXP)}-152c", f"lp-SOL-{int(EXP)}-152_25c"]
    assert db._live_fold_id(g(150.0, 152.5)) == f"lp-SOL-{int(EXP)}-150c-152_5c"


def test_positions_collector_failure_states(tmp_path):
    bad = tmp_path / "live.json"
    bad.write_text("{not json", encoding="utf-8")
    cases = (
        (db.load_live(tmp_path / "none.json"), "<span class='chip ev'>немає даних</span>", "Живих даних ще нема"),
        (db.load_live(bad), "<span class='chip risk'>файл не прочитано</span>", "(JSONDecodeError)"),
        ((_payload(status="error", positions=[]), None), "<span class='chip risk'>позицій не видно</span>", "Bybit не відповів"),
        ((_payload(status="no_key"), None), "позицій не видно", "ключ Bybit лише для читання — напиши Claude"),
        ((_payload(status="<b>odd</b>"), None), "позицій не видно", "Невідомий стан збирача"),
    )
    for (data, err), chip, text in cases:
        page = db.render_positions(data, err, NOW)
        main = _main(page)
        assert chip in _lead(main) and text in main, text
        assert main.index("<div class='lead'>") < main.index("<h2>Позиції" + ICON) and "wide fold" not in main
        assert page.count("<b>Застереження</b>") == 1 and page.index("</main>") < page.index("<b>Застереження</b>")
        assert "aria-current=page>Позиції" in page and page.endswith("</body></html>") and "<b>odd" not in page
    # a partial failure of the collector stays visible above the folds
    part = db.render_live_main(_payload(errors=["tickers ETH: timeout <x>"]), None, NOW)
    assert part.index("Частина запитів до Bybit не вдалась: tickers ETH: timeout &lt;x&gt;") < part.index("wide fold")
    # the collector stopped: the dot, the stamp and the lead all say so
    old = db.render_live_main(_payload(), None, NOW + 3600)
    assert "lp-dot stale" in old and "дані застарілі: збирач, схоже, зупинився" in old and "дані застарілі</span>" in _lead(old)


def _events_snap() -> dict:
    """The snapshot's calendar on 26.10 (ForexFactory rows, as the live snapshot): FOMC as three rows, GDP and Core PCE
    at the same minute, a jobless-claims row that the table leaves out; horizon 14 days."""
    rows = [
        ("2026-10-28T18:00:00+00:00", "fomc", "FOMC Statement"),
        ("2026-10-28T18:00:00+00:00", "fomc", "Federal Funds Rate"),
        ("2026-10-28T18:30:00+00:00", "fomc", "FOMC Press Conference"),
        ("2026-10-29T12:30:00+00:00", "gdp", "Advance GDP q/q"),
        ("2026-10-29T12:30:00+00:00", "core_pce", "Core PCE Price Index m/m"),
        ("2026-10-29T12:30:00+00:00", "jobless_claims", "Unemployment Claims"),
        ("2026-11-06T13:30:00+00:00", "nfp", "Non-Farm Employment Change <script>"),
    ]
    ev = [{"ts_utc": t, "kind": k, "title": n, "impact": "high"} for t, k, n in rows]
    return {"sections": {"events": {"ok": True, "data": {"horizon_days": 14, "events": ev}}}}


SEED = [
    {"ts": 1793210400, "kind": "fomc", "label": "Oct 2026 FOMC"},  # inside the horizon: the feed has it already
    {"ts": 1794317400, "kind": "cpi", "label": "US CPI 2026-11"},  # 10.11, beyond the 14-day horizon
]


def test_live_events_merge_feed_and_seed():
    ev = db.live_events(_events_snap(), SEED, NOW)
    when = [(datetime.fromtimestamp(e["ts"], UTC).strftime("%d.%m %H:%M"), e["label"]) for e in ev]
    assert when == [
        ("28.10 18:00", "ФРС: рішення щодо ставки"),
        ("29.10 12:30", "ВВП США · PCE США (інфляція)"),
        ("06.11 13:30", "NFP США (зайнятість)"),
        ("10.11 13:30", "CPI США (інфляція)"),
    ]
    # without a snapshot only the seed is left, from now on
    assert [e["label"] for e in db.live_events(None, SEED, NOW)] == ["ФРС: рішення щодо ставки", "CPI США (інфляція)"]


def test_breakeven_table_has_event_rows_before_expiry_only():
    groups, _ = db.live_groups(_calls_and_strangle(), NOW)
    ev = db.live_events(_events_snap(), SEED, NOW)
    for g in groups:
        g["events"] = ev
    c84 = db.block_live_be(groups[0])  # expiry 30.10
    assert "⚡ ср 28.10, 20:00 · ФРС: рішення щодо ставки" in c84 and "ВВП США · PCE США" in c84 and "NFP" not in c84
    assert c84.index("ср 28.10, 10:00") < c84.index("ср 28.10, 20:00") < c84.index("чт 29.10, 10:00")
    strangle = db.block_live_be(groups[2])  # expiry 13.11
    assert "NFP США" in strangle and "CPI США" in strangle and "<script>" not in strangle
    page = db.render_live_main(_calls_and_strangle(), None, NOW, events=ev)
    assert page.count("class=lp-ev") >= 6
