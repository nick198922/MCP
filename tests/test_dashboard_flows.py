"""scripts/dashboard.py, the «Потоки» tab: the snapshot's flow_btc / flow_eth sections (the market_flow tool).

The sections are built by the real tool through the snapshot job's own `_call`, on the responses captured 2026-10-28
(tests/fixtures/market_flow_btc_1h_2026-10-28.json, via test_market_flow's transports; liquidations synthetic). No network.
"""

import asyncio
import functools
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

import test_market_flow as tmf  # noqa: E402
from tradedesk.analytics import market_flow as mf  # noqa: E402

ICON = "<span class='info' tabindex='0' role='button'"
ASOF = "2026-10-28T13:10:00Z"  # tmf.END (13:00 UTC) + 10 min, the tool's clock in tmf._flow_rt
NOW = db.parse_utc(ASOF).timestamp() + 300


def _down(req: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="maintenance")


async def _build(down: tuple[str, ...]) -> dict:
    from mcp import Client
    from mcp.server.mcpserver import MCPServer

    from tradedesk.collector.assistant_snapshot import _call
    from tradedesk.tools import register_all

    kw = {"handlers": {n: _down for n in down}}
    if "funding" in down:
        kw["exchange_factory"] = DownEx
    rt = tmf._flow_rt(**kw)
    srv = MCPServer("t")
    register_all(srv, rt)
    try:
        async with Client(srv) as c:
            return await _call(c, "market_flow", {"base": "BTC", "timeframe": "1h", "days": 1, "include_series": True})
    finally:
        await rt.aclose()


class DownEx(tmf.FlowEx):
    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        import ccxt

        raise ccxt.NetworkError("venue down")


@functools.cache
def _section_json(down: tuple[str, ...] = ()) -> str:
    return json.dumps(asyncio.run(_build(down)))


def section(down: tuple[str, ...] = ()) -> dict:
    """A flow section as the snapshot stores it (JSON round trip, as the dashboard reads the file)."""
    return json.loads(_section_json(down))


def snap(**sections) -> dict:
    secs = {k: {**v, "asof_utc": v.get("asof_utc", ASOF)} for k, v in sections.items()}
    return {"schema": 1, "asof_utc": ASOF, "sections": secs, "missing": [k for k, v in secs.items() if not v["ok"]], "stale": []}


def _page(s: dict, asset: str = "BTC") -> str:
    return db.render_flows(s, None, NOW, db.parse_flow_query(f"asset={asset}"))


def test_flow_usd_is_a_copy_of_the_tool_format():
    for x in (0.0, 999.4, -12_345.0, 137_000_000.0, -884_000_000.0, 1_250_703_399.1, -2.5e10):
        assert db.flow_usd(x) == mf.usd_ua(x), x
    assert db.flow_usd(-74e6, signed=False) == "74 млн $" and db.flow_usd(None) == db.flow_usd("x") == "—"


def _lead(page: str) -> str:
    m = re.search(r"<div class='lead'>.*?</div>", page, re.S)
    assert m, "no lead"
    return m.group(0)


def _main(page: str) -> str:
    return page[page.index("<main") : page.index("</main>")]


def test_flows_tab_lead_folds_and_caveats_at_the_bottom():
    """docs/design/PANEL.md rules 1-8 (standing improvements, item 22): the lead first, every block folded with its
    headline number, short «i» notes, the caveats and the tool's notes once at the bottom; numbers are the section's."""
    sec = section()
    assert sec["ok"] and sec["missing"] == [] and len(sec["data"]["series"]) == 24
    d = sec["data"]
    page = _page(snap(flow_btc=sec))
    main = _main(page)
    s24, s4 = d["summary"]["24h"], d["summary"]["4h"]
    for text in (
        "aria-current=page>Потоки",
        "href='/flows?asset=BTC' aria-current=page>BTC",
        "href='/flows?asset=ETH'>ETH",
        db._e(d["sentence_4h_ua"]),
        db.flow_usd(s4["perp_cvd"]),
        db.flow_usd(s24["liq_long"], signed=False),
        db._pc(s24["funding_annualized_mean"] * 100, 1),
        "5 хв тому",
    ):
        assert text in page, text
    # rule 1: the lead before any heading: the tool's 24-hour sentence, its price × OI regime and what to do
    lead = _lead(main)
    assert main.index("<div class='lead'>") < main.index("<h2>")
    assert f"<span class='chip calm'>{db._e(d['quadrant']['24h']['regime_ua'])}</span>" in lead
    assert f"<p class='now'>{db._e(d['sentence_ua'])}</p>" in lead and f"<b>Що робити:</b> {db._e(db.FLOW_CALM_DO)}" in lead
    # rule 4: all eight blocks folded (none open), stable ids for the keep-open script, a one-line summary each
    assert "<details open" not in main and "<details" not in main[: main.index("<section class='wide fold'")]
    ids = re.findall(r"<section class='wide fold' id='(f-\w+)'>", main)
    assert ids == ["f-now", "f-totals", "f-price", "f-cvd", "f-oi", "f-funding", "f-liq", "f-premium"]
    for title in ("Що зараз: 24 і 4 год", "Підсумки", "Ціна", "CVD спот і ф&#x27;ючерси", "Відкритий інтерес, $",
                  "Funding · річних", "Ліквідації лонгів і шортів", "Премія Coinbase"):  # fmt: skip
        assert f"<summary><h2>{title}{ICON}" in main, title
    usd, pc = db.flow_usd, db._pc
    price, oi = pc(s24["price_change"] * 100, 2), pc(s24["oi_change"] * 100, 2)
    last = d["series"][-1]
    assert re.findall(r"<span class='fs'>(.*?)</span>", main) == [
        db._e(f"24 год: {d['quadrant']['24h']['regime_ua']} · 4 год: {d['quadrant']['4h']['regime_ua']}"),
        db._e(f"24 год: ціна {price} · OI {oi} (монети) · CVD ф'ючерси {usd(s24['perp_cvd'])}"),
        f"{db._k(last['price'])} · {price} за 24 год",
        db._e(f"24 год: спот {usd(s24['spot_cvd'])} · ф'ючерси {usd(s24['perp_cvd'])}"),
        f"{usd(last['oi_usd'], signed=False)} · {oi} за 24 год (монети)",
        f"останній {pc(last['funding_annualized'] * 100, 1)} річних · середнє 24 год {pc(s24['funding_annualized_mean'] * 100, 1)}",
        f"24 год: лонги {usd(s24['liq_long'], signed=False)} · шорти {usd(s24['liq_short'], signed=False)}",
        f"до медіани {last['premium_adj'] * 1e4:+.1f} б.п. · середнє 24 год {s24['premium_adj_mean'] * 1e4:+.1f} б.п.".replace("-", "−"),
    ]
    # price, CVD, OI, funding, liquidations, premium: one chart each, the liquidations as bars both sides of zero
    assert page.count("<svg") == 6 and page.count("fill:var(--bad)") >= 1 and page.count("fill:var(--ok)") >= 1
    assert "28.10" in re.findall(r">(\d\d\.\d\d)</text>", page)  # the 00:00 UTC day mark of the window
    # rule 3: every «i» on the tab is the short instruction form
    n = page.count(ICON)
    assert n == 8 and page.count("<span class='do'><i>") == 2 * n and "Для угоди:" not in page
    # rule 5: the caveats and the tool's own notes once, after the main content; no caveat repeated in a block
    assert page.count("<b>Застереження</b>") == 1 and page.index("</main>") < page.index("<b>Застереження</b>")
    for once in ("Описово, не сигнал", "us-demand-flows (премія Coinbase)", "Не інвестиційна порада",
                 f"Примітки інструмента market_flow (англ.): {len(sec['warnings'])}", "правила макета"):  # fmt: skip
        assert page.count(once) == 1 and page.index(once) > page.index("</main>"), once
    for rejected in ("taker-imbalance-8h-continuation", "liquidation-flush-overshoot", "us-demand-flows"):  # (i) and blocks too
        assert rejected not in main and rejected in page, rejected
    assert "не сигнал" not in main and "немає даних" not in page and "не вдалося" not in page and "<script" not in page
    assert "href='/flows'" in db.render_radar(None, "missing", NOW)  # the tab is in the shared nav


def test_flows_lead_cases():
    sec = section()
    # a named combination of the card in either window: its own chip, still no side taken
    pat = json.loads(json.dumps(sec))
    pat["data"]["quadrant"]["4h"]["patterns_ua"] = ["ріст на плечі без спот-попиту (крихкий)"]
    page = _page(snap(flow_btc=pat))
    lead = _lead(page)
    assert "<span class='chip ev'>комбінація картки</span>" in lead and db._e(db.FLOW_PATTERN_DO) in lead
    assert "Комбінація картки за 4 год: ріст на плечі без спот-попиту (крихкий)." in _main(page)
    assert "комбінацій картки: 1" in _main(page)
    # rule 6: a high-impact event within 48 h by the page's clock wins the chip and joins «what now»
    events = {"ok": True, "data": {"events": [{"title": "Oct 2026 FOMC", "impact": "high", "ts_utc": "2026-10-28T18:00:00Z"}]}}
    lead = _lead(_page(snap(flow_btc=pat, events=events)))
    assert "<span class='chip risk'>подія</span>" in lead and "Подія з високим впливом: Oct 2026 FOMC" in lead
    assert "<b>Що робити:</b> до релізу" in lead
    late = db.render_flows(snap(flow_btc=sec, events=events), None, NOW + 86_400, db.parse_flow_query("asset=BTC"))
    assert "Подія" not in _lead(late)  # already past
    # no sentence in either window: an explained lead, the folds still there
    bare = json.loads(json.dumps(sec))
    bare["data"]["sentence_ua"] = bare["data"]["sentence_4h_ua"] = None
    page = _page(snap(flow_btc=bare))
    lead = _lead(page)
    assert "<span class='chip ev'>бракує даних</span>" in lead and "Речення про потоки BTC немає" in lead
    assert _main(page).count("<section class='wide fold'") == 8
    # the 4-hour sentence stands in for a missing 24-hour one
    four = json.loads(json.dumps(sec))
    four["data"]["sentence_ua"] = ""
    assert f"<p class='now'>{db._e(four['data']['sentence_4h_ua'])}</p>" in _lead(_page(snap(flow_btc=four)))


def test_flows_tab_shows_failed_sources_and_never_raises():
    sec = section(("coinalyze", "coinbase"))  # liquidations and premium down, the rest is returned
    assert sec["ok"] and {m["field"] for m in sec["missing"]} >= {"liquidations:binance", "liquidations:bybit", "premium:coinbase"}
    page = _page(snap(flow_btc=sec))
    main = _main(page)
    above = main[: main.index("<section class='wide fold'")]  # the failed sources are visible, right under the lead
    assert "немає даних «ліквідації Binance»" in above and "немає даних «ліквідації Bybit»" in above
    assert "немає даних «свічки Coinbase (премія)»" in above and main.count("немає даних «ліквідації Binance»") == 1
    assert "Ліквідацій немає" in main and "Премії немає" in main
    summaries = dict(zip(re.findall(r"id='(f-\w+)'", main), re.findall(r"<span class='fs'>(.*?)</span>", main), strict=True))
    assert summaries["f-liq"] == summaries["f-premium"] == "немає даних" and summaries["f-cvd"].startswith("24 год: спот")
    assert page.count("<svg") == 4 and "не вдалося" not in page

    dead = section(("binance_spot", "binance_futures", "bybit", "coinbase", "coinalyze", "funding"))
    assert dead["ok"] is False and dead["data"] is None
    eth = _page(snap(flow_btc=sec, flow_eth=dead), "ETH")
    assert "href='/flows?asset=ETH' aria-current=page>ETH" in eth
    lead = _lead(eth)
    assert "<span class='chip ev'>немає даних</span>" in lead and "Даних потоків ETH у знімку немає" in lead
    assert "немає даних «потоки ETH (вкладка)»" in eth and "немає даних «OI Bybit»" in eth and "немає даних «funding Binance»" in eth
    assert "без даних: потоки ETH (вкладка)" in eth and "<svg" not in eth and "<details" not in _main(eth)
    assert eth.count("<b>Застереження</b>") == 1 and eth.index("</main>") < eth.index("<b>Застереження</b>")

    old = _page(snap(radar={"ok": True, "data": {}}), "ETH")  # a snapshot from a collector without these sections
    assert "Секції «потоки ETH (вкладка)» у знімку ще немає" in _lead(old) and "<details" not in _main(old)
    empty = db.render_flows(None, "missing", NOW)
    assert "Знімка асистента ще немає" in empty and "<main" not in empty and empty.endswith("</ul></div></body></html>")
    assert empty.index("aria-current=page>Потоки") < empty.index("<b>Застереження</b>")
    stale = section() | {"stale": True, "asof_utc": "2026-10-28T10:00:00Z"}
    main = _main(_page(snap(flow_btc=stale)))
    assert "з попереднього знімка" in main[: main.index("<section class='wide fold'")]


def test_flows_tab_escapes_text_and_survives_odd_data():
    xss = "<img src=x onerror=alert(1)>"
    sec = section()
    sec["data"]["sentence_ua"] = xss
    sec["data"]["quadrant"]["24h"]["patterns_ua"] = [xss]
    sec["missing"] = [{"field": xss, "provider": "x", "reason": "error", "detail": xss}]
    sec["warnings"] = [xss]
    page = _page(snap(flow_btc=sec))
    assert xss not in page and db._e(xss) in page
    for odd in (
        {"series": "x", "summary": None, "quadrant": 5},
        {"series": [{"ts": "a"}, None, {"ts": 1, "price": "nan"}], "summary": {"24h": {"price_change": "x"}}, "window": []},
        {"series": [{"ts": 0, "price": float("inf")}, {"ts": 3_600_000, "price": True}]},
    ):
        page = _page(snap(flow_btc={"ok": True, "data": odd, "missing": [], "warnings": []}))
        assert page.endswith("</body></html>") and "Знімок не вдалося" not in page, odd
    assert db.parse_flow_query("asset=eth") == {"asset": "ETH"} and db.parse_flow_query("asset=<x>") == {"asset": "BTC"}


def test_flows_page_reads_the_snapshot_file(tmp_path, monkeypatch):
    p = tmp_path / "snapshot.json"
    p.write_text(json.dumps(snap(flow_btc=section())), encoding="utf-8")
    monkeypatch.setattr(db, "SNAPSHOT_PATH", p)
    page = db.flows_page("asset=BTC", now=NOW)
    assert "querySelectorAll('.info')" in page and page.endswith("</body></html>") and page.count("<svg") == 6
    assert "Секції «потоки ETH (вкладка)» у знімку ще немає" in db.flows_page("asset=ETH", now=NOW)


def test_flow_chart_svg_bars_and_empty():
    ts = [86_400_000.0 * 3 + i * 3_600_000 for i in range(-2, 3)]
    svg = db.flow_chart_svg(
        ts, [], "liq", db._flow_axis_usd, bars=(([1e6, None, 0, 2e6, 5e5], "red", 1), ([0, 3e6, None, 0, 1e6], "blue", -1))
    )
    assert svg.count("<rect") == 5 and ">04.01</text>" in svg
    assert db.flow_chart_svg(ts, [([None] * 5, "")], "x", db._flow_axis_usd) == ""
    assert db.flow_chart_svg(ts[:1], [([1.0], "")], "x", db._flow_axis_usd) == ""
    assert db._flow_axis_usd(1.5e9, 5e8) == "1.5 млрд" and db._flow_axis_usd(-2e8, 1e8) == "−0.2 млрд"
    assert db._flow_axis_usd(250e6, 50e6) == "250 млн" and db._flow_axis_usd(40e3, 1e4) == "40 тис"
