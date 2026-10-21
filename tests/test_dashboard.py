"""scripts/dashboard.py: the project dashboard (pure helpers and the page)."""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

H = 3600.0


def test_usage_eta_from_samples_after_the_last_reset():
    now = 100 * 86400.0
    samples = [(now - 30 * H, 90.0), (now - 26 * H, 3.0), (now - 20 * H, 10.0), (now - 2 * H, 28.0), (now, 30.0)]
    t = db.usage_eta(samples, now, 97.0)
    # within the last 24 h: 10 % at -20 h, 30 % now -> 24 % a day; 67 points left -> 67 h
    assert t["rate_per_day"] == pytest.approx(24.0)
    assert t["eta"] == pytest.approx(now + 67 / 24 * 86400)
    assert db.usage_eta([(now, 50.0)], now, 97.0) == {"rate_per_day": None, "eta": None}
    assert db.usage_eta([(now - 10 * H, 98.0), (now, 98.5)], now, 97.0)["eta"] is None


def test_history_rows_newest_first_without_markup():
    lines = [
        "2026-10-17_005046\tdelivered\t✅ **Агенти здали крок:** `agent/x`\t⏱ 0 год 49 хв · ліміт",
        "2026-10-17_014721\terror\t❌ збій",
        "broken line",
    ]
    rows = db.history_rows(lines)
    assert [r["status"] for r in rows] == ["error", "delivered"]
    assert rows[1]["when"] == "17.10 00:50" and rows[1]["text"] == "✅ Агенти здали крок: agent/x" and rows[1]["cost"].startswith("⏱")


def test_table_freshness_reads_seconds_millis_and_days(tmp_path):
    p = tmp_path / "t.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE funding_hist (ts INTEGER)")
    con.execute("CREATE TABLE ohlcv_hist (ts INTEGER)")
    con.execute("CREATE TABLE trader_snapshot (day TEXT)")
    now = 1_800_000_000.0
    con.execute("INSERT INTO funding_hist VALUES (?)", (int(now - 600),))
    con.execute("INSERT INTO ohlcv_hist VALUES (?)", (int((now - 10 * 86400) * 1000),))
    con.execute("INSERT INTO trader_snapshot VALUES ('2027-01-15')")
    con.commit()
    con.close()
    rows = {r["table"]: r for r in db.table_freshness(p, now)}
    assert rows["funding_hist"]["ok"] is True and rows["funding_hist"]["age_s"] == pytest.approx(600)
    assert rows["ohlcv_hist"]["ok"] is False and rows["funding_hist"]["rows"] == 1
    assert "trader_snapshot" in rows and "basis_hist" not in rows
    assert rows["trader_snapshot"]["age_s"] == pytest.approx(8 * H)  # 2027-01-15 08:00 UTC; a day row counts from midnight
    assert db.table_freshness(tmp_path / "none.sqlite", now) == []


def test_render_has_all_sections_and_escapes():
    snap = {
        "generated_at": 1_800_000_000.0,
        "agents": {
            "line": "🟢 Працюють над кроком з 10:10",
            "activity": "квант (10:47) працює: «<script>»",
            "work": ("agent/2026-10-17-x", 2),
            "parked": [("agent/2026-10-17-fx", "17.10 20:30")],
            "awaiting_review": ["agent/2026-10-17-y"],
            "counts_24h": {"delivered": 11, "error": 1},
            "history": [{"when": "17.10 08:31", "status": "delivered", "text": "здано", "cost": "⏱ 1 год"}],
            "progress": ["**10:47 квант:** Running stage 0"],
            "doctor": "",
        },
        "usage": {
            "five_hour": {"pct": 66.0, "resets_at": "2026-10-17T09:10:00+00:00"},
            "seven_day": {"pct": 85.0, "resets_at": "2026-10-20T17:00:00+00:00"},
            "trend": {"rate_per_day": 18.6, "eta": 1_800_100_000.0, "eta_owner_only": None, "limit": 97.0, "owner_only_at": 80.0},
        },
        "cards": [
            {
                "name": "IDEA-20261017-forward-paper-carry",
                "title": "паперовий carry",
                "status": "accepted",
                "verdict": "",
                "owner": True,
                "order": 2,
                "report": "",
            },
            {
                "name": "IDEA-20261016-session-stop-hunt",
                "title": "SSH",
                "status": "rejected",
                "verdict": "−0.214R",
                "owner": True,
                "order": None,
                "report": "",
            },
        ],
        "progress": {
            "done": 30,
            "total": 90,
            "pct": 33.3,
            "groups": [{"label": "Фінал: прогін і обгортка", "done": 0, "total": 5}],
            "cards_left": 40,
            "pace": 8.0,
            "eta_days": 5.0,
        },
        "commits": [["abc1234", "17.10 12:00", "Owner's decisions"]],
        "questions": ["Q-1: ключ?"],
        "services": [{"label": "MCP", "unit": "tradedesk-mcp", "state": "active"}],
        "machine": {
            "mem_total": 12 * 1024**3,
            "mem_used": 1024**3,
            "load": ["0.1", "0.1", "0.1"],
            "uptime_s": 86400.0,
            "disk_used": 22 * 1024**3,
            "disk_total": 193 * 1024**3,
            "db_bytes": 100 * 1024**2,
            "backup_age_s": 3600.0,
        },
        "tables": [{"label": "funding", "table": "funding_hist", "rows": 12345, "age_s": 600.0, "ok": True}],
    }
    page = db.render(snap)
    for text in (
        "Прогрес проєкту",
        "Фінал: прогін і обгортка",
        "Агенти зараз",
        "Ліміт Claude",
        "Черга",
        "Результати перевірок",
        "Кроки агентів",
        "Сервер",
        "Колектор даних",
        "Останні зміни в main",
        "Питання агентів до тебе",
        "паперовий carry",
        "12 345",
        "agent/2026-10-17-y",
    ):
        assert text in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "http-equiv='refresh'" in page and 'class="bar"' in page
    assert "href='/brief'" in page  # the «Бриф» tab


def test_read_usage_prefers_the_agents_cache_and_throttles_own_calls(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    cache = tmp_path / "usage.json"
    cache.write_text(_json.dumps({"five_hour": {"utilization": 12.0, "resets_at": "x"}, "seven_day": {"utilization": 65.0}}))
    monkeypatch.setattr(db, "USAGE_CACHE", cache)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/.credentials.json").write_text(_json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
    monkeypatch.setattr(db, "HOME", tmp_path)
    calls = []
    monkeypatch.setattr(db.urllib.request, "urlopen", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("429")))
    now = _time.time()
    assert db.read_usage(now)["seven_day"]["pct"] == 65.0 and calls == []  # fresh cache: no call
    old = now + 1500  # cache 25 min old: one own call (fails), then the cache is still used under an hour
    db._usage_last_call[0] = 0.0
    assert db.read_usage(old)["five_hour"]["pct"] == 12.0 and calls == [1]
    assert db.read_usage(old + 60)["five_hour"]["pct"] == 12.0 and calls == [1]  # throttled: no second call


def test_project_progress_counts_owner_card_steps_and_milestones():
    cards = [
        {"owner": True, "status": "testing", "steps_done": 2, "steps_total": 5},
        {"owner": True, "status": "rejected", "steps_done": 0, "steps_total": 0},
        {"owner": True, "status": "accepted", "steps_done": 0, "steps_total": 0},
        {"owner": False, "status": "accepted", "steps_done": 0, "steps_total": 9},
    ]
    md = "# P\n\n## Основа\n- [x] a\n- [x] b\n\n## Фінал\n- [ ] c\n- [ ] d\n"
    p = db.project_progress(cards, md, pace=2.0)
    assert p["groups"][0]["done"] == 3 and p["groups"][0]["total"] == 7
    assert (p["done"], p["total"]) == (5, 11) and round(p["pct"], 1) == 45.5
    assert p["cards_left"] == 4 and p["eta_days"] == 2.0
    assert db.project_progress(cards, "", pace=0.0)["eta_days"] is None


def test_delivered_pace_counts_recent_deliveries_only():
    now = datetime(2026, 10, 17, 12, 0).timestamp()
    lines = ["2026-10-16_100000\tdelivered\tx", "2026-10-15_100000\tcontinues\ty", "2026-10-01_100000\tdelivered\tz"]
    assert db.delivered_pace(lines, now, days=7) == 1 / 7


# ---------------------------------------------------------------- «Бриф» tab (assistant snapshot)

SNAP_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "assistant_snapshot_2026-10-20.json"


def _snap() -> tuple[dict, float]:
    snap, err = db.load_snapshot(SNAP_FIXTURE)
    assert err is None
    return snap, db.parse_utc(snap["asof_utc"]).timestamp()


def test_brief_renders_the_real_snapshot():
    snap, asof = _snap()
    page = db.render_brief(snap, None, asof + 300)
    for text in (
        "Головне сьогодні",
        "BTC за 120 днів і що закладають опціони",
        "Сценарії на 27.11",
        "Волатильність",
        "Події · час Київ",
        "Funding · річних",
        "Страх і жадібність",
        "16-й перцентиль за рік",
        "5 хв тому",
        "ризик-нейтральні",
        "vrp-real-marks",
        "Не інвестиційна порада",
        "Oct 2026 FOMC",
        "Hyperliquid",
        "беззбиток стреддлу до 23.10",
        "aria-current=page>Бриф",
    ):
        assert text in page, text
    assert page.count("<svg") == 2 and "старший за 30 хв" not in page and "Блок не вдалося" not in page
    if db.KYIV is not None:
        assert "28.10 20:00" in page  # 18:00 UTC FOMC in Kyiv summer time
    assert "<script" not in page and "googleapis" not in page  # no JS needed, no CDN


def test_range_probabilities_match_the_analytics_reference():
    from tradedesk.analytics.options import Smile
    from tradedesk.analytics.probability import prob_above

    snap, _ = _snap()
    F, T, pts = db.chain_smile(snap["sections"]["chain"]["data"])
    smile = Smile(F, T, [k for k, _ in pts], [v for _, v in pts])
    for K in [60000.0, 70000.0, 80000.0, F, 90000.0, 110000.0]:
        assert db.prob_above(pts, F, T, K) == pytest.approx(prob_above(smile, K), abs=1e-9)
    cuts = db.scenario_cuts(F)
    assert cuts == [70000.0, 75000.0, 80000.0, 85000.0, 90000.0, 95000.0]
    bands = db.range_probabilities(pts, F, T, cuts)
    assert len(bands) == 7 and all(b["p"] >= 0 for b in bands)
    assert sum(b["p"] for b in bands) == pytest.approx(1.0)
    assert bands[3]["lo"] == 80000.0 and bands[3]["p"] == max(b["p"] for b in bands)  # the forward's band is the likeliest


def test_brief_missing_and_corrupt_snapshot(tmp_path, monkeypatch):
    snap, err = db.load_snapshot(tmp_path / "none.json")
    assert (snap, err) == (None, "missing")
    monkeypatch.setattr(db, "SNAPSHOT_PATH", tmp_path / "none.json")
    page = db.brief_page(now=1_800_000_000.0)
    assert "Знімка асистента ще немає" in page and "assistant_snapshot" in page and "Не інвестиційна порада" in page
    for raw in ("{bad", "[1, 2]", '{"sections": []}', ""):
        p = tmp_path / "snap.json"
        p.write_text(raw, encoding="utf-8")
        assert db.load_snapshot(p) == (None, "corrupt")
        monkeypatch.setattr(db, "SNAPSHOT_PATH", p)
        assert "пошкоджений" in db.brief_page(now=1_800_000_000.0)


def test_brief_warns_on_stale_snapshot_and_carried_sections():
    snap, asof = _snap()
    snap["sections"]["chain"]["stale"] = True
    snap["sections"]["chain"]["asof_utc"] = "2026-10-20T18:00:00Z"
    snap["stale"] = ["chain"]
    snap["sections"]["ohlc"] = {
        "ok": False,
        "data": None,
        "missing": [{"reason": "error", "detail": "market_ohlcv: timeout"}],
        "warnings": [],
    }
    snap["missing"] = ["ohlc"]
    page = db.render_brief(snap, None, asof + 2 * 3600)
    assert "старший за 30 хв" in page and "2 год тому" in page
    assert "з попереднього знімка: ланцюжок опціонів" in page and "без даних: свічки BTC" in page
    assert "Свічок BTC у знімку немає" in page and "market_ohlcv: timeout" in page
    assert "Сценарії на 27.11" in page and "Блок не вдалося" not in page  # the carried chain still renders


def test_brief_escapes_hostile_strings_from_the_snapshot():
    snap, asof = _snap()
    s = snap["sections"]
    evil = "<script>alert(1)</script>"
    s["events"]["data"]["events"][0]["title"] = evil
    s["events"]["data"]["next_event"]["title"] = evil
    s["events"]["warnings"] = ["<img src=x onerror=alert(2)>"]
    s["tickers"]["data"]["tickers"][0]["symbol"] = "<img src=x onerror=alert(3)>/USDT:USDT"
    s["funding_btc"]["data"]["venues"][0]["venue"] = "<b>evil</b>"
    s["skew"]["data"]["expiries"][2]["expiry"] = "<i>x</i>"
    s["vrp"]["warnings"] = [evil]
    s["term"]["data"]["shape"] = "<svg onload=alert(4)>"
    s["fng"]["data"]["latest"]["zone"] = "<u>z</u>"
    s["ohlc"] = {"ok": False, "data": None, "missing": [{"detail": "<iframe src=//x.example>"}], "warnings": []}
    snap["missing"] = ["<svg onload=alert(5)>"]
    snap["schema"] = "<b>2</b>"
    page = db.render_brief(snap, None, asof + 60)
    for raw in ("<script", "<img", "<iframe", "<b>evil", "<i>x", "<u>z", "<svg onload", "<b>2"):
        assert raw not in page, raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;iframe" in page and "&lt;b&gt;evil" in page


def test_brief_survives_malformed_sections():
    snap, asof = _snap()
    s = snap["sections"]
    s["tickers"]["data"] = {"tickers": "x"}
    s["ohlc"]["data"]["rows"] = [[1, "a"], None, "z"]
    s["chain"]["data"]["strikes"] = [None, 1, {"strike": "x"}, {"strike": 80000.0, "put": {"iv": float("nan")}}]
    s["term"]["data"]["expiries"] = [{"days": "a"}, 3]
    s["events"]["data"] = {"events": [1, None, {"title": 5}], "next_event": "soon"}
    s["funding_btc"]["data"]["venues"] = 5
    s["vrp"]["data"]["current"] = []
    s["fng"]["data"]["latest"] = "x"
    s["skew"] = "broken"
    snap["expiries"] = None
    snap["asof_utc"] = "yesterday"
    page = db.render_brief(snap, None, asof)
    assert "Час знімка невідомий" in page
    assert "Свічок BTC у знімку немає" in page and "Ланцюжка опціонів у знімку немає" in page
    assert "Funding · річних" in page and "Hyperliquid" in page  # ETH funding is intact
    assert "Сценарії на місячну експірацію" in page  # no expiry label without its data


def test_brief_no_expiry_label_when_the_chain_was_dropped():
    snap, asof = _snap()
    snap["sections"]["chain"] = {"ok": False, "data": None, "missing": [{"detail": "too old"}], "warnings": []}
    page = db.render_brief(snap, None, asof)  # `expiries.month` still says 27NOV26
    assert "Сценарії на місячну експірацію" in page and "Сценарії на 27.11" not in page


def test_expiry_utc_parses_deribit_tokens():
    assert db.expiry_utc("27NOV26").isoformat() == "2026-11-27T08:00:00+00:00"
    assert db.expiry_utc("6NOV26").day == 6
    assert db.expiry_utc("31FEB26") is None and db.expiry_utc("bad") is None and db.expiry_utc(None) is None


# ---------------------------------------------------------------- «Опціони» tab (strategy builder on the snapshot chain)


def _legs(strat: str, qty: float = 0.1) -> tuple[dict, list[dict]]:
    snap, _ = _snap()
    ch = db.option_chain(snap)
    legs, err = db.option_legs(ch, strat, db.default_strikes(ch, strat), qty)
    assert err is None
    return ch, legs


def test_options_tab_renders_the_real_snapshot():
    snap, asof = _snap()
    page = db.render_options(snap, None, asof + 300, db.parse_options_query(""))
    for text in (
        "Конструктор · BTC, експірація 27.11 · 37.5 дн · форвард 83 215",
        "aria-current=page>Опціони",
        "value='long_strangle' aria-pressed=true",
        "value='77000' selected",  # 25Δ put
        "value='90000' selected",  # 25Δ call
        "Платиш премію",
        "$291",  # long 25Δ strangle on 0.1 BTC, as in the card («≈ $291 (2.9 %)»)
        "$74.1k / $92.9k",
        "Макс. прибуток",
        "необмежений",
        "Ймовірність прибутку",
        "32 %",
        "Ризик на угоду: <b class='num'>$100</b>",
        "більше за ризик у 2.9 раза",
        "до <b class='num'>0.03 BTC</b>",
        "ризик-нейтральна",
        "vrp-real-marks",
        "Не інвестиційна порада",
        "5 хв тому",
        "localStorage",
        "name='dep' min='100' max='1e+08' step='100' value='10000'",
        "name='riskp' min='0.1' max='100' step='0.1' value='1'",
        "name='qty' min='0.01' max='100' step='0.01' value='0.1'",
    ):
        assert text in page, text
    assert page.count("<svg") == 1 and "старший за 30 хв" not in page and "не вдалося" not in page
    assert "googleapis" not in page and "<script src" not in page  # inline JS only, no CDN
    assert "href='/options'" in db.render_brief(snap, None, asof)  # the tab is in the shared nav


def test_options_numbers_match_the_analytics_reference():
    from tradedesk.analytics import strategy

    for strat in db.OPT_STRATEGIES:
        ch, legs = _legs(strat)
        F, T, iv = ch["F"], ch["T"], ch["iv"]
        mine, ref = db.payoff_extremes(legs), strategy.payoff_extremes(legs)
        for key in ("max_profit", "max_loss"):
            assert (mine[key] is None) == (ref[key] is None) and (ref[key] is None or mine[key] == pytest.approx(ref[key], abs=1e-6))
        prices = [F * 0.3 + F * 2.2 * i / 20000 for i in range(20001)]
        pnl = strategy.payoff_curve(legs, prices)
        assert all(db.payoff(legs, p) == pytest.approx(v, abs=1e-9) for p, v in zip(prices[::500], pnl[::500], strict=True))
        ref_be = strategy.breakevens(prices, pnl)
        assert db.payoff_breakevens(legs) == pytest.approx(ref_be, abs=1e-6)
        ref_pop = strategy.prob_of_profit(legs, prices, pnl, forward=F, days=ch["days"], iv=iv)
        assert db.prob_of_profit(legs, F, T, iv) == pytest.approx(ref_pop, abs=1e-9)
    # long + short of the same structure are complementary
    ch, ls = _legs("long_strangle")
    F, T, iv = ch["F"], ch["T"], ch["iv"]
    _, ss = _legs("short_strangle")
    assert db.prob_of_profit(ls, F, T, iv) + db.prob_of_profit(ss, F, T, iv) == pytest.approx(1.0)


def test_options_formulas_against_analytic_references():
    F, T, iv = 100.0, 30 / 365, 0.5
    # long straddle at K = F, premium P: breakevens K ± P, PoP = 1 − [Φ(z(K+P)) − Φ(z(K−P))]
    legs = [
        {"kind": "call", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 4.0},
        {"kind": "put", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 3.0},
    ]
    assert db.payoff_breakevens(legs) == pytest.approx([93.0, 107.0])
    v = iv * T**0.5

    def z(x: float) -> float:
        return db._ncdf((db.math.log(x / F) + 0.5 * v * v) / v)

    assert db.prob_of_profit(legs, F, T, iv) == pytest.approx(1 - (z(107.0) - z(93.0)), abs=1e-12)
    assert db.payoff_extremes(legs) == {"max_profit": None, "max_loss": pytest.approx(-7.0)}
    # iron condor 80/90/110/120 for a 4.5 credit: max profit 4.5, max loss 10 − 4.5, breakevens 85.5 / 114.5
    condor = [
        {"kind": "put", "side": "long", "size": 1.0, "strike": 80.0, "entry_premium": 1.0},
        {"kind": "put", "side": "short", "size": 1.0, "strike": 90.0, "entry_premium": 4.0},
        {"kind": "call", "side": "short", "size": 1.0, "strike": 110.0, "entry_premium": 2.0},
        {"kind": "call", "side": "long", "size": 1.0, "strike": 120.0, "entry_premium": 0.5},
    ]
    assert db.payoff_extremes(condor) == {"max_profit": pytest.approx(4.5), "max_loss": pytest.approx(-5.5)}
    assert db.payoff_breakevens(condor) == pytest.approx([85.5, 114.5])
    assert db.prob_of_profit(condor, F, T, iv) == pytest.approx(z(114.5) - z(85.5), abs=1e-12)
    # risk budget: $10 000 × 1 % = $100; a $291 loss on 0.1 BTC is 2.9× it, 0.03 BTC fits
    fit = db.risk_fit(-290.74, 0.1, 10000.0, 1.0)
    assert fit["budget"] == pytest.approx(100.0) and fit["fits"] is False and fit["ratio"] == pytest.approx(2.9074)
    assert fit["qmax"] == 0.03 and fit["pct_dep"] == pytest.approx(2.9074)
    assert db.risk_fit(-50.0, 0.1, 10000.0, 1.0)["fits"] is True and db.risk_fit(-100.0, 0.5, 10000.0, 1.0)["qmax"] == 0.5
    assert db.risk_fit(None, 0.1, 10000.0, 1.0) == {"budget": 100.0, "unbounded": True}


def test_options_strategies_and_query_parameters():
    snap, asof = _snap()
    # switching the strategy resets the strikes to its defaults; the condor gets 10Δ wings
    page = db.render_options(snap, None, asof, db.parse_options_query("s=iron_condor&cur=long_strangle&kput=80000&qty=1"))
    assert "value='iron_condor' aria-pressed=true" in page and "Захисний пут" in page
    assert "value='70000' selected" in page and "value='97000' selected" in page and "value='77000' selected" in page
    assert "Отримуєш премію" in page and "$1 940" in page  # 1 BTC condor credit
    # a strike change within the same strategy is kept
    page = db.render_options(snap, None, asof, db.parse_options_query("cur=short_strangle&kput=75000&kcall=92000&dep=50000&riskp=2"))
    assert "value='75000' selected" in page and "value='92000' selected" in page and "ризик необмежений" in page
    assert "Ризик на угоду: <b class='num'>$1 000</b>" in page
    # straddles sit at the strike nearest the forward, the selects are locked
    page = db.render_options(snap, None, asof, db.parse_options_query("s=short_straddle&kput=60000"))
    assert "value='83000' selected" in page and " disabled>" in page
    # a condor with crossed strikes is explained, not raised
    page = db.render_options(snap, None, asof, db.parse_options_query("cur=iron_condor&kputw=80000&kput=77000"))
    assert "Кондор: захисний пут &lt; пут" in page and "<svg" not in page
    # hostile or out-of-range parameters fall back to the defaults
    p = db.parse_options_query("s=<script>&kput=nan&dep=-5&riskp=1e9&qty=inf&" + "x=1&" * 60)
    assert p["strat"] == "long_strangle" and p["dep"] == 10000.0 and p["riskp"] == 1.0 and p["qty"] == 0.1 and p["kput"] is None
    page = db.render_options(snap, None, asof, db.parse_options_query("s=<script>alert(1)</script>"))
    assert page.count("<script") == 1 and "alert(1)" not in page  # only our own inline script


def test_options_missing_snapshot_chain_and_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SNAPSHOT_PATH", tmp_path / "none.json")
    page = db.options_page("s=long_straddle", now=1_800_000_000.0)
    assert "Знімка асистента ще немає" in page and "Не інвестиційна порада" in page and "aria-current=page>Опціони" in page
    p = tmp_path / "snap.json"
    p.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(db, "SNAPSHOT_PATH", p)
    assert "пошкоджений" in db.options_page(now=1_800_000_000.0)
    snap, asof = _snap()
    snap["sections"]["chain"] = {"ok": False, "data": None, "missing": [{"detail": "deribit_option_chain: timeout"}], "warnings": []}
    page = db.render_options(snap, None, asof)
    assert "Ланцюжка опціонів у знімку немає" in page and "deribit_option_chain: timeout" in page
    snap, asof = _snap()
    snap["sections"]["chain"]["data"]["strikes"] = [None, 1, {"strike": "x"}, {"strike": 80000.0, "put": {"mark_usd": float("nan")}}]
    assert "Ланцюжка опціонів у знімку немає" in db.render_options(snap, None, asof)
    snap, asof = _snap()
    page = db.render_options(snap, None, asof + 2 * 3600)
    assert "старший за 30 хв" in page and "2 год тому" in page and "Платиш премію" in page


def test_options_escapes_hostile_strings_from_the_snapshot():
    snap, asof = _snap()
    snap["sections"]["chain"]["data"]["expiry"] = "<img src=x onerror=alert(1)>"
    snap["sections"]["chain"]["stale"] = True
    snap["sections"]["chain"]["asof_utc"] = "<script>alert(2)</script>"
    snap["missing"] = ["<svg onload=alert(3)>"]
    page = db.render_options(snap, None, asof)
    for raw in ("<img", "<script>alert", "<svg onload"):
        assert raw not in page, raw
    assert "&lt;img src=x" in page and "Платиш премію" in page


# ---------------------------------------------------------------- «Журнал» tab

T0 = 1_790_000_000
J_COLS = "opened_ts, closed_ts, symbol, side, size, entry, thesis, tags, status, exit, outcome, pnl, r_multiple, note"
R_COLS = (
    "opened_ts, currency, structure, expiry_ts, spot, forward, t_years, legs, premium_per_btc, entry_fees_per_btc, max_loss_per_btc,"
    " risk_basis, size_btc, deposit_usd, risk_pct, risk_usd, within_risk, status, pnl_usd, pnl_pct_spot, r_worst_case, r_study,"
    " source, trigger"
)


def _jrow(day: int, symbol: str, side: str, tags: str, status: str, outcome: str | None, pnl: float | None, r=None, thesis="t", note=None):
    closed = T0 + day * 86400 + 3600 if status == "closed" else None
    return (T0 + day * 86400, closed, symbol, side, 0.01, 80000.0, thesis, tags, status, 81000.0, outcome, pnl, r, note)


def _rrow(day: int, status: str, pnl=None, pct=None, rw=None, rs=None, source="method", trigger="chain", structure="long_strangle_25d"):
    return (
        T0 + day * 86400, "BTC", structure, T0 + (day + 40) * 86400, 80000.0, 80100.0, 40 / 365, "[]", 2900.0, 48.0, 2950.0,
        "max_loss", 0.1, 10000.0, 1.0, 295.0, 0, status, pnl, pct, rw, rs, source, trigger,
    )  # fmt: skip


def _journal_db(path: Path, journal: list[tuple] | None, rec: list[tuple] | None, versions=None) -> Path:
    """A store built from the real migrations; ``versions`` limits them (e.g. without v16 = no rec_paper)."""
    from tradedesk.store.schema import MIGRATIONS

    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if versions is None or v in versions:
            for sql in MIGRATIONS[v]:
                con.execute(sql)
    con.executemany(f"INSERT INTO journal ({J_COLS}) VALUES ({','.join('?' * 14)})", journal or [])
    if rec is not None:
        con.executemany(f"INSERT INTO rec_paper ({R_COLS}) VALUES ({','.join('?' * 24)})", rec)
    con.commit()
    con.close()
    return path


def _sample_journal() -> list[tuple]:
    return [
        _jrow(0, "BTCUSDT", "long", '["bybit", "import", "linear"]', "closed", "win", 10.0, note="bybit:linear:1"),
        _jrow(1, "ETHUSDT", "short", '["bybit", "import", "linear"]', "closed", "loss", -100.0),
        _jrow(2, "SOLUSDT", "long", '["binance", "import", "usdm"]', "closed", "win", 30.0),
        _jrow(3, "BTC-31OCT26-90000-C", "long", '["bybit", "import", "option"]', "closed", "win", 20.0, r=0.5),
        _jrow(4, "HYPE", "long", '["hyperliquid", "import", "perp", "airdrop"]', "closed", "win", 500.0),
        _jrow(5, "BTC", "long", '["pp-paper", "carry"]', "closed", "loss", -5.0),
        _jrow(6, "ETHUSDT", "long", '["setup:breakout", "conf:2"]', "open", None, None, thesis="пробій рейнджу"),
    ]


def _sample_rec() -> list[tuple]:
    return [
        _rrow(0, "settled", 50.0, 0.02, 0.4, 0.5),
        _rrow(1, "settled", -30.0, -0.01, -0.3, -0.3, source="manual", trigger=None),
        _rrow(2, "open"),
    ]


def test_journal_tab_renders_trades_sources_and_paper_recs(tmp_path):
    p = _journal_db(tmp_path / "s.sqlite", _sample_journal(), _sample_rec())
    data, err = db.load_journal(p)
    assert err is None and len(data["journal"]) == 7 and len(data["rec_paper"]) == 3
    page = db.render_journal(data, None, float(T0 + 10 * 86400), path=p)
    # real closed trades: +10 −100 +30 +20 (airdrop and *-paper rows left out); 3 of 4 in profit; drawdown 100 after the loss
    for text in (
        "aria-current=page>Журнал",
        "лише читання",
        "<b>Застереження</b>",
        "Угод (закритих)</span><span class='v'>4</span>",
        "75 %",
        "−$40",
        "−$10",
        "Найбільша просадка</span><span class='v'>−$100</span>",
        "+0.50 R",
        "Відкритих</span><span class='v'>1</span>",
        "<td>Bybit</td><td class='num'>2</td>",
        "<td>Binance</td>",
        "<td>Bybit опціони</td>",
        "Паперові carry-угоди (теги *-paper): 1 рядків",
        "Рядків з тегом airdrop: 1",
        "пробій рейнджу",
        "setup:breakout, conf:2",
        "відкрита з",
        "сторінка 1 з 1 · 5 угод",
        "USD, усі біржі й опціони разом",
        "USD, одна крива P&L усіх бірж і опціонів",
        # paper recommendations: the main tiles are the method chain group only (+50, 2 % of spot, 1 open);
        # the manual trade (−30) is shown on its own line
        "гіпотеза</span>",
        "−0.59 % спота",
        "Розраховано</span><span class='v'>1</span>",
        "метод за ланцюгом · відкритих 1",
        "+2.00 %",
        "+0.40 R",
        "+$50",
        "порівнянні з тестом H1), як method",
        "Ручні угоди (обрані людиною чи моделлю, не метод): розраховано 1, відкритих 0; у плюсі 0 %, "
        "середнє −1.00 % спота, P&amp;L разом −$30.",
        "ланцюг (як у тесті H1)",
        "вручну",
    ):
        assert text in page, text
    assert "+0.50 %" not in page and "Додаткові угоди методу" not in page  # no mixed mean, no empty group line
    assert "HYPE" not in page and "<svg" not in page and "<script" not in page
    snap, asof = _snap()
    assert "href='/journal'" in db.render_brief(snap, None, asof)  # the tab is in the shared nav


def test_journal_summaries_match_the_analytics_reference(tmp_path):
    from tradedesk.analytics import journal as aj
    from tradedesk.analytics import rec_paper as ar

    data, _ = db.load_journal(_journal_db(tmp_path / "s.sqlite", _sample_journal(), _sample_rec()))
    real = [t for t in data["journal"] if db.journal_kind(db.journal_tags(t["tags"]))[0] == "real"]
    mine, ref = db.journal_summary(real), aj.summary(real)
    for key in ("closed_trades", "wins", "losses", "win_rate", "total_pnl", "expectancy_per_trade", "avg_r", "r_count"):
        assert mine[key] == pytest.approx(ref[key]), key
    settled = [t for t in data["rec_paper"] if t["status"] == "settled"]
    assert db.rec_summary(data["rec_paper"]) == pytest.approx(ar.summarize(settled))
    assert db.journal_summary([]) == aj.summary([]) and db.rec_summary([]) == ar.summarize([])
    assert db.max_drawdown_usd(real) == pytest.approx(100.0) and db.max_drawdown_usd([]) == 0.0
    # tags: the kind and the source label
    assert db.journal_kind(["hyperliquid", "import", "perp", "airdrop"]) == ("airdrop", "Hyperliquid")
    assert db.journal_kind(["cc-paper"]) == ("paper", "cc-paper") and db.journal_kind([]) == ("real", "без біржі")
    assert db.journal_tags("{bad") == [] and db.journal_tags('{"a": 1}') == [] and db.journal_tags(None) == []


def test_journal_missing_db_tables_and_empty_states(tmp_path):
    none = tmp_path / "none.sqlite"
    data, err = db.load_journal(none)
    assert data is None and err == "missing" and not none.exists()  # read-only: nothing is created
    page = db.render_journal(data, err, float(T0), path=none)
    assert "Бази ще немає" in page and "Не інвестиційна порада" in page and "aria-current=page>Журнал" in page
    # an old store: journal without rec_paper (schema before v16)
    p = _journal_db(tmp_path / "old.sqlite", _sample_journal(), None, versions=range(1, 16))
    page = db.render_journal(*db.load_journal(p), float(T0))
    assert "Таблиці rec_paper у базі ще немає" in page and "Угод (закритих)" in page
    # no journal table at all
    p = tmp_path / "bare.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE funding_hist (ts INTEGER)")
    con.close()
    page = db.render_journal(*db.load_journal(p), float(T0))
    assert "Таблиці journal у базі ще немає" in page and "Таблиці rec_paper" in page
    # empty tables: how to add the first trade; open-only paper recs
    p = _journal_db(tmp_path / "empty.sqlite", [], [_rrow(0, "open")])
    page = db.render_journal(*db.load_journal(p), float(T0))
    assert "Угод ще немає." in page and "journal_add" in page
    assert "Розрахованих паперових угод методу за ланцюгом (порівнянних з H1) ще немає (відкритих: 1)" in page
    p = _journal_db(tmp_path / "empty2.sqlite", [], [])
    assert "assistant_paper записує їх щоранку" in db.render_journal(*db.load_journal(p), float(T0))
    # not a database
    p = tmp_path / "junk.sqlite"
    p.write_bytes(b"not a database at all" * 100)
    data, err = db.load_journal(p)
    assert data is None and err.startswith("unreadable")
    assert "Базу не вдалося прочитати" in db.render_journal(data, err, float(T0))


def test_rec_paper_main_tiles_use_the_tools_method_group(tmp_path):
    """Main tiles = assistant_paper_status 'method' (source method, trigger NULL or 'chain'); the triggered and the
    manual trades are summarized separately, never mixed in."""
    from tradedesk.analytics import rec_paper as ar

    rec = [
        _rrow(0, "settled", 50.0, 0.02, 0.4, 0.5),
        _rrow(1, "settled", -10.0, -0.004, -0.1, -0.1, trigger=None),  # method with NULL trigger: H1-comparable
        _rrow(2, "settled", 100.0, 0.05, 0.8, 1.0, trigger="regime"),
        _rrow(3, "settled", 80.0, 0.04, 0.6, 0.8, trigger="event"),
        _rrow(4, "settled", -30.0, -0.01, -0.3, -0.3, source="manual", trigger=None),
        _rrow(5, "open"),
        _rrow(6, "open", trigger="event"),
    ]
    data, _ = db.load_journal(_journal_db(tmp_path / "s.sqlite", [], rec))
    rows = data["rec_paper"]
    groups = db.rec_groups(rows)
    # the tool's definitions (src/tradedesk/tools/assistant_paper.py, summary_settled)
    meth = [x for x in rows if x["source"] == "method"]
    ref = {
        "method": [x for x in meth if x.get("trigger") in (None, "chain")],
        "method_triggered": [x for x in meth if x.get("trigger") not in (None, "chain")],
        "manual": [x for x in rows if x["source"] == "manual"],
    }
    for key in ref:
        assert [t["id"] for t in groups[key]] == [t["id"] for t in ref[key]], key
        settled = [t for t in ref[key] if t["status"] == "settled"]
        assert db.rec_summary(groups[key]) == pytest.approx(ar.summarize(settled)), key
    assert [db.rec_summary(groups[k])["n"] for k in ref] == [2, 2, 1]
    page = db.block_rec_paper(rows)
    for text in (
        "Розраховано</span><span class='v'>2</span>",
        "метод за ланцюгом · відкритих 1",
        "+0.80 %",  # (2 % − 0.4 %) / 2: method group only
        "+$40",
        "Додаткові угоди методу (зміна режиму / подія; перекриваються з ланцюгом, корельовані): розраховано 2, "
        "відкритих 1; у плюсі 100 %, середнє +4.50 % спота, P&amp;L разом +$180.",
        "Ручні угоди (обрані людиною чи моделлю, не метод): розраховано 1, відкритих 0; у плюсі 0 %, "
        "середнє −1.00 % спота, P&amp;L разом −$30.",
    ):
        assert text in page, text
    assert "+1.92 %" not in page  # the mixed mean over all five settled trades


def test_load_journal_os_error_is_reported_not_raised(tmp_path, monkeypatch):
    p = _journal_db(tmp_path / "s.sqlite", _sample_journal(), _sample_rec())

    def denied(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "exists", denied)
    data, err = db.load_journal(p)
    assert data is None and err == "unreadable: PermissionError"
    assert "Базу не вдалося прочитати" in db.render_journal(data, err, float(T0))


def test_load_journal_connection_is_read_only(tmp_path, monkeypatch):
    p = _journal_db(tmp_path / "s.sqlite", _sample_journal(), _sample_rec())
    seen = {}
    real_connect = sqlite3.connect

    def spy(database, *a, **k):
        con = real_connect(database, *a, **k)
        seen.update(uri=database, kw=k)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("INSERT INTO journal (opened_ts, symbol, side, status) VALUES (1, 'X', 'long', 'open')")
        return con

    monkeypatch.setattr(db.sqlite3, "connect", spy)
    data, err = db.load_journal(p)
    assert err is None and len(data["journal"]) == 7
    assert seen["uri"].startswith("file:") and seen["uri"].endswith("?mode=ro") and seen["kw"].get("uri") is True


def test_journal_pages_and_query(tmp_path, monkeypatch):
    rows = [_jrow(i, f"S{i:03d}", "long", '["bybit", "import", "linear"]', "closed", "win", 1.0) for i in range(120)]
    p = _journal_db(tmp_path / "s.sqlite", rows, [])
    monkeypatch.setattr(db, "JOURNAL_DB", p)
    page = db.journal_page("", now=float(T0))
    assert "сторінка 1 з 3 · 120 угод" in page and "href='/journal?page=2'>старіші" in page and "новіші" not in page
    assert "S119" in page and "S069" not in page  # newest first, 50 a page
    page = db.journal_page("page=3", now=float(T0))
    assert "S019" in page and "S020" not in page and "href='/journal?page=2'>← новіші" in page and "старіші" not in page
    assert "сторінка 3 з 3" in db.journal_page("page=999", now=float(T0))  # clamped to the last page
    for q in ("page=0", "page=-1", "page=²", "page=<x>", "page=1e3", "x=1&" * 40):
        assert db.parse_journal_query(q) == {"page": 1}, q
    assert db.parse_journal_query("page=2") == {"page": 2}
    assert [db._px(x) for x in (82076.6, 3.4412, 0.0123, 0.00001234, None)] == ["82 076.60", "3.4412", "0.0123", "0.00001234", "—"]


def test_journal_escapes_hostile_strings_from_the_database(tmp_path):
    hostile = [
        _jrow(0, "<script>alert(1)</script>", "<b>x</b>", '["bybit", "<svg onload=alert(3)>"]', "closed", "win", 5.0,
              thesis="<img src=x onerror=alert(2)>", note="</td><iframe src=//e>"),
    ]  # fmt: skip
    rec = [_rrow(0, "settled", 1.0, 0.001, 0.1, 0.1, structure="<iframe>", trigger="<b>t</b>")]
    p = _journal_db(tmp_path / "s.sqlite", hostile, rec)
    page = db.render_journal(*db.load_journal(p), float(T0), path=tmp_path / "<i>.sqlite")
    for raw in ("<script>alert", "<img", "<svg onload", "<iframe", "<b>x", "<b>t", "<i>.sqlite"):
        assert raw not in page, raw
    assert "&lt;script&gt;alert(1)" in page and "&lt;img src=x" in page and "&lt;svg onload" in page
