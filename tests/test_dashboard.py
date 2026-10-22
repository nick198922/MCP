"""scripts/dashboard.py: the project dashboard (pure helpers and the page)."""

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
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


def _project_snap() -> dict:
    return {
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


def test_render_has_all_sections_and_escapes():
    page = db.render(_project_snap())
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
    assert "«<script>»" not in page and "«&lt;script&gt;»" in page  # the only <script> is the tooltip one
    assert page.count("<script>") == 1 and "querySelectorAll('.info')" in page
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


def test_options_greeks_and_scenarios_match_the_analytics_reference():
    from tradedesk.analytics import strategy

    snap, _ = _snap()
    rows = {r["strike"]: r for r in snap["sections"]["chain"]["data"]["strikes"]}
    for strat in db.OPT_STRATEGIES:
        ch, legs = _legs(strat, qty=0.3)
        F, T, days, iv = ch["F"], ch["T"], ch["days"], ch["iv"]
        mine = db.position_greeks(legs, F, T, iv)
        ref = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        deribit = dict(ref)
        for leg in legs:  # aggregate_greeks takes one IV, so it is applied leg by leg at the leg's own IV
            g = strategy.aggregate_greeks([leg], forward=F, days=days, iv=leg["iv"])
            sign = 1 if leg["side"] == "long" else -1
            dg = rows[leg["strike"]][leg["kind"]]
            for k in ref:
                ref[k] += g[k]
                deribit[k] += sign * leg["size"] * dg[k]
        for k in ref:
            assert mine[k] == pytest.approx(ref[k], rel=1e-9, abs=1e-12), (strat, k)
            assert mine[k] == pytest.approx(deribit[k], rel=0.02, abs=2e-3 if k == "delta" else 1e-9), (strat, k)  # Deribit's own greeks
        for s in (F * 0.8, F, F * 1.13):
            for left, d_iv in ((days, 0.0), (days, 0.05), (days - 10, 0.0), (0.5, -0.03)):
                want = sum(
                    strategy.mtm_value([lg], forward=s, days=left, iv=lg["iv"] + d_iv)
                    - strategy.mtm_value([lg], forward=F, days=days, iv=lg["iv"])
                    for lg in legs
                )
                assert db.value_change(legs, s, left, F, T, iv, d_iv) == pytest.approx(want, abs=1e-6)
        sc = db.scenario_rows(legs, ch, "27.11")
        assert [r["label"] for r in sc] == ["сьогодні", "сьогодні, IV +5 п.", "через 10 дн", "через 20 дн", "на експірацію 27.11"]
        prices = [F * (1 + m / 100) for m in db.OPT_MOVES]
        assert sc[-1]["pnl"] == pytest.approx(strategy.payoff_curve(legs, prices), abs=1e-9)
        assert sc[0]["pnl"][db.OPT_MOVES.index(0)] == pytest.approx(0.0, abs=1e-9)  # today, no move: exactly zero


def test_options_greeks_against_analytic_references():
    # ATM straddle, F = K = 100, σ = 0.5, 30 d: Δ = 2Φ(v/2) − 1, vega = 2·F·φ(v/2)·√T / 100, θ = −2·F·φ·σ / (2√T) / 365
    F, days, iv = 100.0, 30.0, 0.5
    T = days / 365
    legs = [
        {"kind": "call", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0, "iv": None},
        {"kind": "put", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0, "iv": None},
    ]
    v = iv * T**0.5
    phi = db._npdf(v / 2)
    G = db.position_greeks(legs, F, T, iv)  # no leg IV: the ATM IV is used
    assert G["delta"] == pytest.approx(2 * db._ncdf(v / 2) - 1, abs=1e-12)
    assert G["vega"] == pytest.approx(2 * F * phi * T**0.5 / 100, rel=1e-12)
    assert G["theta"] == pytest.approx(-2 * F * phi * iv / (2 * T**0.5) / 365, rel=1e-12)
    assert G["gamma"] == pytest.approx(2 * phi / (F * v), rel=1e-12)
    # +5 vol points ≈ 5 × vega (vega of an ATM straddle is almost linear in σ); a short day ≈ theta
    assert db.value_change(legs, F, days, F, T, iv, 0.05) == pytest.approx(5 * G["vega"], rel=0.01)
    assert db.value_change(legs, F, days - 1, F, T, iv) == pytest.approx(G["theta"], rel=0.03)
    # at expiry the model value is the intrinsic one: value_change = intrinsic − model price now
    now = db._b76(F, 100.0, T, iv, True) + db._b76(F, 100.0, T, iv, False)
    assert db.value_change(legs, 112.0, 0.0, F, T, iv) == pytest.approx(12.0 - now, abs=1e-12)
    # the slider: whole days, clamped to the last day before expiry
    assert db.horizon_days(10, 37.46) == 10 and db.horizon_days(99, 37.46) == 37 and db.horizon_days(-3, 37.46) == 0
    assert db.horizon_days(5, 0.4) == 0
    assert db.parse_options_query("hzn=12.7")["hzn"] == 12 and db.parse_options_query("hzn=abc")["hzn"] == db.OPT_HZN_DEFAULT


def test_options_tab_shows_greeks_verdict_vol_and_scenarios():
    snap, asof = _snap()
    page = db.render_options(snap, None, asof + 60, db.parse_options_query(""))
    for text in (
        "Греки позиції",
        "Вега</td><td class='r num ok' style='white-space:nowrap'>+$17",
        "Тета</td><td class='r num bad' style='white-space:nowrap'>−$8",
        "зросте на 1 пункт",
        "Що це означає",
        "Ти купуєш волатильність за <b class='num'>$291</b> на 0.10 BTC",
        "нижче <b class='num'>$74.1k</b> або вище <b class='num'>$92.9k</b>",
        "Чи дорога зараз волатильність",
        "16-й",
        "<tr class=cur><td>0%-20% · зараз",
        "відхилено (H1)",
        "лише гіпотеза",
        "ризик-нейтральні",
        "Сценарії · P&amp;L позиції, USD",
        "сьогодні, IV +5 п.",
        "через 20 дн",
        "на експірацію 27.11",
        "type='range' id='hzn' name='hzn' min='0' max='37' step='1' value='10'",
        "через 10 дн (ті самі IV)",
        "stroke-dasharray:6 4",  # the «through N days» line
    ):
        assert text in page, text
    page = db.render_options(snap, None, asof, db.parse_options_query("s=short_strangle&hzn=99"))
    assert "Ти продаєш волатильність" in page and "Ризик необмежений" in page and "value='37'" in page
    # no VRP section: the vol block still shows the chain's ATM IV, the verdict skips the DVOL line
    snap["sections"]["vrp"] = {"ok": False, "data": None, "missing": [{"detail": "deribit_vrp: timeout"}], "warnings": []}
    page = db.render_options(snap, None, asof)
    assert "Чи дорога зараз волатильність" in page and "ATM IV 27.11" in page and "deribit_vrp: timeout" in page
    assert "перцентилі за рік" not in page and "не вдалося" not in page
    # hostile VRP strings are escaped
    snap, asof = _snap()
    snap["sections"]["vrp"]["data"]["by_dvol_level"][0]["dvol_percentile_365d"] = "<img src=x onerror=alert(1)>"
    snap["sections"]["vrp"]["data"]["current"]["dvol_percentile_365d"] = "<b>x</b>"
    page = db.render_options(snap, None, asof)
    assert "<img" not in page and "&lt;img src=x" in page and "<b>x" not in page and "не вдалося" not in page


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
        "Угод (закритих)<span class='info'",
        "<span class='v'>4</span>",
        "75 %",
        "−$40",
        "−$10",
        "Найбільша просадка<span class='info'",
        "<span class='v'>−$100</span>",
        "+0.50 R",
        f"Відкритих{db._info('j_open')}</span><span class='v'>1</span>",
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
        f"Розраховано{db._info('rec_n')}</span><span class='v'>1</span>",
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
        f"Розраховано{db._info('rec_n')}</span><span class='v'>2</span>",
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


# ---------------------------------------------------------------- «Журнал» tab: the assistant's trade plans

P_COLS = (
    "created_ts, exchange, symbol, instrument, side, entry_type, entry, stop, target, expires_ts, verdict, tags, status, outcome, r_result"
)


def _prow(i: int, verdict: str, status: str = "open", outcome: str | None = None, r: float | None = None, tags: str = '["src:own"]'):
    return (T0 + i * 3600, "binance", "BTC/USDT:USDT", "perp", "long", "limit", 80000.0, 79000.0, 82000.0, T0 + 86400 * 3,
            verdict, tags, status, outcome, r)  # fmt: skip


def _plan_db(path: Path, plans: list[tuple], versions=None) -> Path:
    _journal_db(path, [], [], versions=versions)
    con = sqlite3.connect(path)
    con.executemany(f"INSERT INTO trade_plan ({P_COLS}) VALUES ({','.join('?' * 15)})", plans)
    con.commit()
    con.close()
    return path


def _sample_plans() -> list[tuple]:
    return [
        _prow(0, "enter", "resolved", "target", 1.9),
        _prow(1, "enter", "resolved", "stop", -1.05),
        _prow(2, "enter", "resolved", "expired", 0.3),
        _prow(3, "enter", "resolved", "not_filled", None),
        _prow(4, "enter"),
        _prow(5, "wait", "resolved", "stop", -1.0),
        _prow(6, "skip"),
        _prow(7, "enter", "resolved", "target", 2.0, tags='["src:demo-shadow", "demo:taken"]'),  # out of the summary
    ]


def test_journal_tab_summarizes_trade_plans(tmp_path):
    p = _plan_db(tmp_path / "s.sqlite", _sample_plans())
    data, err = db.load_journal(p)
    assert err is None and len(data["trade_plan"]) == 7 and data["plan_shadow"] == 1
    page = db.render_journal(data, None, float(T0 + 86400), path=p)
    for text in (
        "Плани угод асистента<span class='info'",
        "<span class='chip calm'>облік</span>",
        "src:demo-shadow) до підсумку не входять",
        # counts by verdict and status (the shadow plan is not counted)
        "<td>enter (✅ входити)</td><td class='num'>5</td><td class='num'>1</td><td class='num'>4</td>",
        "<td>wait (⏳ чекати)</td><td class='num'>1</td><td class='num'>0</td><td class='num'>1</td>",
        "<td>skip (❌ пропустити)</td><td class='num'>1</td><td class='num'>1</td><td class='num'>0</td>",
        # resolved enter: R 1.9, −1.05, 0.3 (+ one never filled): mean +0.38 R, 2 of 3 in profit, CI wide around 0
        f"Розв'язано{db._info('plan_n')}</span><span class='v'>4</span><span class='s '>з входом 3 · з R 3",
        "67 %",
        "+0.38 R",
        "95 % ДІ містить 0: переваги не видно",
        "1 / 1</span><span class='s '>ціль / стоп · за строком 1 · без входу 1",
        "Замало планів: n = 3 планів enter з R (потрібно ≥ 30)",
        "Тіньових планів демо-трейдера (src:demo-shadow): 1",
    ):
        assert text in page, text
    assert "+0.67 R" not in page  # the shadow plan's +2.0 R is not mixed into the mean


def test_trade_plan_stats_match_the_analytics_reference(tmp_path):
    from tradedesk.analytics.trade_plan import summarize_plans

    rs = [1.9 if i % 3 else -1.05 for i in range(31)]  # 31 R values: df 30 and the normal quantile beyond it
    for plans, vs0 in (
        (_sample_plans(), "95 % ДІ містить 0"),
        ([_prow(i, "enter", "resolved", "target" if v > 0 else "stop", v) for i, v in enumerate(rs)], "95 % ДІ вище 0"),
        ([_prow(i, "enter", "resolved", "stop", -1.0 - 0.01 * i) for i in range(3)], "95 % ДІ нижче 0"),
        ([_prow(0, "enter", "resolved", "stop", -1.0)], "ДІ — від 2 планів з R"),
    ):
        p = _plan_db(tmp_path / f"s{len(plans)}.sqlite", plans)
        data, _ = db.load_journal(p)
        mine = db.plan_stats([x for x in data["trade_plan"] if x["status"] == "resolved" and x["verdict"] == "enter"])
        ref = summarize_plans(data["trade_plan"])["by_verdict"]["enter"]
        for key in ("n", "n_filled", "outcomes", "hit_rate", "win_rate", "mean_r", "median_r", "ci95_mean_r"):
            assert mine[key] == pytest.approx(ref[key]), key
        page = db.block_trade_plans(data["trade_plan"], data["plan_shadow"])
        assert vs0 in page, vs0
        assert ("Замало планів" in page) == (mine["n_r"] < 30)
    assert [db.t975(d) for d in (0, 1, 30, 31)] == [float("inf"), 12.706, 2.042, 1.96]


def test_journal_tab_trade_plans_empty_and_old_store(tmp_path):
    p = _plan_db(tmp_path / "empty.sqlite", [])
    data, err = db.load_journal(p)
    assert err is None and data["trade_plan"] == [] and data["plan_shadow"] == 0
    page = db.render_journal(data, None, float(T0))
    assert "Планів асистента ще немає" in page and "plan_add" in page and "Тіньових планів" not in page
    # only shadow plans: still empty for the assistant, the shadow count shown
    p = _plan_db(tmp_path / "shadow.sqlite", [_prow(0, "enter", tags='["src:demo-shadow"]')])
    page = db.render_journal(*db.load_journal(p), float(T0))
    assert "Планів асистента ще немає" in page and "Тіньових планів демо-трейдера (src:demo-shadow): 1" in page
    # open plans only: counts, no outcome yet, the small-sample note
    p = _plan_db(tmp_path / "open.sqlite", [_prow(0, "enter"), _prow(1, "wait")])
    page = db.render_journal(*db.load_journal(p), float(T0))
    assert "язаних планів enter ще немає" in page and "Замало планів: n = 0" in page
    # a store before schema v17: no trade_plan table
    p = _journal_db(tmp_path / "old.sqlite", _sample_journal(), _sample_rec(), versions=range(1, 17))
    data, err = db.load_journal(p)
    assert err is None and data["trade_plan"] is None
    assert "Таблиці trade_plan у базі ще немає" in db.render_journal(data, err, float(T0))


# ---- «Журнал прогнозів»: forecast_log (schema v20), risk-neutral range forecasts settled at delivery

F_COLS = (
    "day, issued_ts, asset, kind, expiry_ts, spot, forward, atm_iv, t_years, lo, hi, prob, ref_prob, covered, status,"
    " settled_ts, delivery_price, outcome, brier"
)
FC_E1 = int(datetime(2026, 10, 30, 8, tzinfo=UTC).timestamp())  # ISO 2026-W44
FC_E2 = FC_E1 + 7 * 86400  # 2026-W45
FC_E3 = FC_E1 + 14 * 86400  # still open
FC_REF = {"above_spot": 0.5, "in_1sigma": 0.6827}
# (asset, expiry) -> kind -> outcome; prob by issue day 7 and 6 days before the expiry
FC_OUTCOMES = {
    ("BTC", FC_E1): {"above_spot": 1, "in_1sigma": 1, "in_pct_5": 1, "in_pct_10": 1},
    ("ETH", FC_E1): {"above_spot": 0, "in_1sigma": 1, "in_pct_5": 0, "in_pct_10": 1},
    ("BTC", FC_E2): {"above_spot": 0, "in_1sigma": 0, "in_pct_5": 0, "in_pct_10": 1},
    ("ETH", FC_E2): {"above_spot": 1, "in_1sigma": 1, "in_pct_5": 1, "in_pct_10": 1},
}
FC_PROBS = {"above_spot": (0.527, 0.48), "in_1sigma": (0.718, 0.70), "in_pct_5": (0.734, 0.585), "in_pct_10": (0.951, 1.0)}


def _frow(asset: str, kind: str, expiry: int, days_before: int, prob: float, outcome: int | None) -> tuple:
    issued = expiry - days_before * 86400
    day = datetime.fromtimestamp(issued, UTC).date().isoformat()
    settled = outcome is not None
    return (
        day, issued, asset, kind, expiry, 80000.0, 80100.0, 0.35, days_before / 365, 76000.0, 84000.0, prob,
        FC_REF.get(kind), 1, "settled" if settled else "open", expiry + 3600 if settled else None,
        81000.0 if settled else None, outcome, (prob - outcome) ** 2 if settled else None,
    )  # fmt: skip


def _sample_forecasts(with_settled: bool = True) -> list[tuple]:
    rows = []
    if with_settled:
        for (asset, exp), kinds in FC_OUTCOMES.items():
            for kind, o in kinds.items():
                for i, d in enumerate((7, 6)):
                    rows.append(_frow(asset, kind, exp, d, FC_PROBS[kind][i], o))
    for asset in ("BTC", "ETH"):
        for kind in FC_PROBS:
            rows.append(_frow(asset, kind, FC_E3, 7, FC_PROBS[kind][0], None))
    return rows


def _fc_db(path: Path, forecasts: list[tuple], versions=None) -> Path:
    p = _journal_db(path, [], [], versions)
    con = sqlite3.connect(p)
    con.executemany(f"INSERT INTO forecast_log ({F_COLS}) VALUES ({','.join('?' * 19)})", forecasts)
    con.commit()
    con.close()
    return p


def test_journal_tab_shows_forecast_brier_calibration_and_weeks(tmp_path):
    from tradedesk.analytics import forecast_log as af

    p = _fc_db(tmp_path / "f.sqlite", _sample_forecasts())
    data, err = db.load_journal(p)
    assert err is None and len(data["forecast_log"]) == 40
    page = db.render_journal(data, None, float(FC_E2 + 86400), path=p)
    ref = af.score([x for x in data["forecast_log"] if x["status"] == "settled"])
    assert ref["n"] == 32 and ref["n_expiries"] == 4
    for text in (
        f"<h2>Журнал прогнозів{db._info('fc')}",
        "Ризик-нейтральні ймовірності, не прогноз асистента.",
        "розмір вибірки — кількість експірацій (n_expiries)",
        f"Прогнозів розраховано{db._info('fc_n')}</span><span class='v'>32</span>",
        "відкритих 8",
        f"Експірацій{db._info('fc_expiries')}</span><span class='v'>4</span>",
        "замало для висновку",
        "Замало експірацій: n_expiries = 4 (потрібно ≥ 30)",
        f"<span class='v'>{ref['brier']:.3f}</span>",
        f"Brier {ref['brier_on_ref']:.3f} проти {ref['brier_ref']:.3f} · 16 прогнозів",
        f"Brier {ref['brier']:.3f} проти {ref['brier_climatology']:.3f}",
        f"Калібрування, 10 кошиків{db._info('fc_calib')}",
        'aria-label="Калібрування: прогноз проти частоти події"',
        "90–100 %",
        f"За тижнями{db._info('fc_weekly')}",
        "<td class='num'>2026-W44</td>",
        "<td class='num'>2026-W45</td>",
        "Brier ±1σ",
    ):
        assert text in page, text
    assert page.index("2026-W45") < page.index("2026-W44")  # newest week first
    assert "Блок не вдалося показати" not in page and "<script" not in page
    # one calibration row per non-empty bin of the analytics reference (prob 1.0 falls into the last bin)
    for b in ref["calibration"]:
        assert f"{100 * b['lo']:.0f}–{100 * b['hi']:.0f} %</td><td class='num'>{b['n']}</td>" in page, b


def test_forecast_rv30_comparison_matches_the_analytics_and_renders(tmp_path):
    from tradedesk.analytics import forecast_log as af

    p = _fc_db(tmp_path / "f.sqlite", _sample_forecasts())
    con = sqlite3.connect(p)
    # the RV30 model: a bit wider than the options on in-range events, 0.5 on above_spot (zero drift)
    con.execute("UPDATE forecast_log SET prob_rv = CASE kind WHEN 'above_spot' THEN 0.5 ELSE MIN(1.0, prob * 1.05) END")
    con.execute("UPDATE forecast_log SET prob_rv = NULL WHERE asset = 'ETH' AND expiry_ts = ?", (FC_E2,))  # no RV30 that day
    con.commit()
    con.close()
    data, err = db.load_journal(p)
    rows = data["forecast_log"]
    mine, ref = db.fc_compare(rows), af.compare(rows)
    assert ref["n"] == 24 and ref["n_dates"] == 2 and ref["enough"] is False
    assert {k: v for k, v in mine.items() if k != "ci95"} == pytest.approx({k: v for k, v in ref.items() if k != "ci95"})
    assert mine["ci95"] == pytest.approx(ref["ci95"])
    assert db.fc_compare([]) == af.compare([]) == {"n": 0, "n_dates": 0}
    assert tuple(db.FC_T975) == af.T975
    page = db.render_journal(data, None, float(FC_E2 + 86400), path=p)
    for text in (
        f"Опціони проти RV30{db._info('fc_rv')}",
        f"{ref['diff']:+.3f}</span>",
        f"Brier {ref['brier_risk_neutral']:.3f} проти {ref['brier_rv30']:.3f} · 95 % ДІ {ref['ci95'][0]:+.3f} … {ref['ci95'][1]:+.3f} · 2 дат експірації",
    ):
        assert text in page, text
    # a store before schema v23 (no prob_rv column) still reads, and the tile is left out
    old = _fc_db(tmp_path / "v22.sqlite", _sample_forecasts(), versions=range(1, 23))
    data, err = db.load_journal(old)
    assert err is None and all(x["prob_rv"] is None for x in data["forecast_log"])
    assert "Опціони проти RV30" not in db.render_journal(data, None, float(FC_E2 + 86400), path=old)


def test_forecast_scores_match_the_analytics_reference(tmp_path):
    from tradedesk.analytics import forecast_log as af

    data, _ = db.load_journal(_fc_db(tmp_path / "f.sqlite", _sample_forecasts()))
    rows = data["forecast_log"]
    mine, ref = db.fc_score(rows), af.score(rows)
    assert set(mine) == set(ref)
    for key in ref:
        if key != "calibration":
            assert mine[key] == pytest.approx(ref[key]), key
    assert len(mine["calibration"]) == len(ref["calibration"])
    for a, b in zip(mine["calibration"], ref["calibration"], strict=True):
        assert a == pytest.approx(b)
    assert db.fc_calibration(rows) == db.fc_score(rows)["calibration"]
    mw, rw = db.fc_weekly(rows), af.weekly(rows)
    assert [w["week"] for w in mw] == [w["week"] for w in rw] == ["2026-W44", "2026-W45"]
    for a, b in zip(mw, rw, strict=True):
        assert a["brier_by_kind"] == pytest.approx(b["brier_by_kind"])
        assert {k: v for k, v in a.items() if k != "brier_by_kind"} == pytest.approx({k: v for k, v in b.items() if k != "brier_by_kind"})
    assert db.fc_score([]) == af.score([]) == {"n": 0} and db.fc_weekly([]) == af.weekly([]) == []
    for ts in (FC_E1, FC_E2, int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())):
        assert db.fc_iso_week(ts) == af.iso_week(ts)
    assert tuple(db.FC_KINDS) == af.KINDS and db.FC_N_BINS == af.N_BINS


def test_journal_tab_forecasts_empty_open_only_and_missing_table(tmp_path):
    # the table exists but is empty
    data, err = db.load_journal(_fc_db(tmp_path / "e.sqlite", []))
    assert err is None and data["forecast_log"] == []
    page = db.render_journal(data, None, float(T0))
    assert f"<h2>Журнал прогнозів{db._info('fc')}" in page and "Прогнозів ще немає" in page and "forecast_log" in page
    assert "Калібрування, 10 кошиків" not in page and "<svg" not in page
    # only open forecasts: nothing settled yet, the first delivery date
    page = db.render_journal(*db.load_journal(_fc_db(tmp_path / "o.sqlite", _sample_forecasts(with_settled=False))), float(T0))
    assert "Розрахованих прогнозів ще немає (відкритих: 8)" in page and db._day(FC_E3) in page
    # a store before schema v20: no forecast_log table
    p = _journal_db(tmp_path / "old.sqlite", _sample_journal(), _sample_rec(), versions=range(1, 20))
    data, err = db.load_journal(p)
    assert err is None and data["forecast_log"] is None
    page = db.render_journal(data, err, float(T0))
    assert "Таблиці forecast_log у базі ще немає" in page and "Угод (закритих)" in page
    # an odd row (non-numeric prob) does not take the page down
    con = sqlite3.connect(tmp_path / "e.sqlite")
    con.execute(f"INSERT INTO forecast_log ({F_COLS}) VALUES ({','.join('?' * 19)})", _frow("BTC", "<b>x", FC_E1, 7, 0.5, 1))
    con.execute("UPDATE forecast_log SET prob = 'abc'")
    con.commit()
    con.close()
    page = db.render_journal(*db.load_journal(tmp_path / "e.sqlite"), float(T0))
    assert "Блок не вдалося показати" in page and "<b>x" not in page and "Мої угоди" in page


# ---------------------------------------------------------------- «Polymarket» tab

PM_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pm_gap_snapshot_2026-10-21.json"
PM_B = 1792581631  # the latest of the two snapshots in the fixture (the other is 1792563645)
PM_END = 1792598400  # end of both markets that pass the threshold at PM_B (12:00 ET, 21.10)


def _pm_rows() -> tuple[list[str], list[dict]]:
    import json

    d = json.loads(PM_FIXTURE.read_text(encoding="utf-8"))
    return d["columns"], [dict(zip(d["columns"], r, strict=True)) for r in d["rows"]]


def _pm_db(path: Path, rows: list[dict], cols: list[str], extra_cols: tuple[str, ...] = (), upto: int | None = 17) -> Path:
    """A store from the real migrations up to ``upto`` (17 = before book_ts / surface_ts / model_forward; None = all)
    with the given pm_gap_snapshot rows; ``extra_cols`` are added when the migrations did not add them."""
    from tradedesk.store.schema import MIGRATIONS

    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if upto is None or v <= upto:
            for sql in MIGRATIONS[v]:
                con.execute(sql)
    have = {r[1] for r in con.execute("PRAGMA table_info(pm_gap_snapshot)")}
    for c in extra_cols:
        if c not in have:
            con.execute(f"ALTER TABLE pm_gap_snapshot ADD COLUMN {c} REAL")
    allc = [*cols, *extra_cols]
    con.executemany(
        f"INSERT INTO pm_gap_snapshot ({', '.join(allc)}) VALUES ({','.join('?' * len(allc))})",
        [[r.get(c) for c in allc] for r in rows],
    )
    con.commit()
    con.close()
    return path


def _pm_base(rows: list[dict], market_id: str = "5690024") -> dict:
    return next(r for r in rows if r["ts"] == PM_B and r["market_id"] == market_id)


def test_pm_copies_match_the_analytics_reference_on_real_rows():
    from dataclasses import replace

    from tradedesk.analytics import pm_gap_paper as P
    from tradedesk.analytics.pm_touch import fee_per_share
    from tradedesk.analytics.probability import prob_touch

    _, rows = _pm_rows()
    acc = db.pm_accepted_hashes(rows)
    assert acc == P.accepted_rule_hashes(rows) and len(acc) >= 2
    for r in rows:
        if r["rules"]:
            assert db.pm_rules_accepted(r["rules"], r["asset"]) == P.rules_accepted(r["rules"], r["asset"]), r["market_id"]
    rule = P.EntryRule()
    assert (db.PM_PASS, db.PM_MIN_DEPTH_USD, db.PM_MIN_ASK, db.PM_MAX_ASK) == (
        rule.threshold,
        rule.min_depth_usd,
        rule.min_ask,
        rule.max_ask,
    )
    assert (db.PM_MIN_HOURS, db.PM_MAX_HOURS, db.PM_VOL_KS, db.PM_MAX_SKEW_S) == (
        rule.min_hours_to_end, rule.max_hours_to_end, rule.vol_ks, rule.max_quote_skew_s,
    )  # fmt: skip
    n_elig = 0
    for r in rows:
        assert db.pm_eligible(r, acc) == P.eligible(r, rule, acc), r["market_id"]
        n_elig += db.pm_eligible(r, acc)
        for k in (1.0, 0.92):
            mine, ref = db.pm_model_prob(r, k), P.model_prob(r, k)
            assert (mine is None) == (ref is None) and (mine is None or mine == pytest.approx(ref, abs=1e-12)), (r["market_id"], k)
    assert n_elig >= 30
    # the same sides with the same numbers, at the registered threshold and below it
    n_seen = {}
    for th in (0.0, 0.03, 0.05):
        ref = {(r["ts"], s["market_id"], s["side"]): s for r in rows for s in P.entry_signals(r, replace(rule, threshold=th), acc)}
        mine = {(r["ts"], s["market_id"], s["side"]): s for r in rows for s in db.pm_sides(r, acc) if s["edge"] >= th}
        assert mine.keys() == ref.keys(), th
        for key, s in mine.items():
            for f in ("edge", "cost", "p_win", "ask", "depth_usd"):
                assert s[f] == pytest.approx(ref[key][f], abs=1e-12), (key, f)
        n_seen[th] = len(mine)
    assert n_seen[0.05] == 2 and n_seen[0.03] > n_seen[0.05] and n_seen[0.0] > n_seen[0.03]
    for p, rate, ex in ((0.79, 0.07, 1.0), (0.1, 0.25, 2.0), (0.5, 0.0, 1.0)):
        assert db.pm_fee(p, rate, ex) == pytest.approx(fee_per_share(p, rate, ex))
    for F, K, s, T in ((100.0, 110.0, 0.6, 3 / 365), (100.0, 90.0, 0.5, 7 / 365), (100.0, 100.0, 0.5, 0.01), (100.0, 120.0, 0.0, 0.1)):
        assert db.pm_prob_touch(F, K, s, T) == pytest.approx(prob_touch(F, K, s, T), abs=1e-12)


def test_pm_filters_skew_horizon_rules_and_book():
    from tradedesk.analytics import pm_gap_paper as P

    _, rows = _pm_rows()
    acc = db.pm_accepted_hashes(rows)
    base, rule = _pm_base(rows), P.EntryRule()
    cases = {
        "synced": ({"book_ts": PM_B + 10, "surface_ts": PM_B - 49}, True),
        "skew": ({"book_ts": PM_B + 12, "surface_ts": PM_B - 49}, False),  # 61 s apart
        "one_time_only": ({"book_ts": PM_B + 500, "surface_ts": None}, True),  # rows without both times pass
        "too_close": ({"end_ts": PM_B + 7199}, False),
        "too_far": ({"end_ts": PM_B + 14 * 86400 + 1}, False),
        "rules": ({"rules_hash": "not-accepted"}, False),
        "uncovered": ({"model_covered": 0}, False),
        "touched": ({"model_note": "touched"}, False),
    }
    for name, (patch, ok) in cases.items():
        r = {**base, **patch}
        assert db.pm_eligible(r, acc) is ok and P.eligible(r, rule, acc) is ok, name
    # book filters: less than $200 at the ask, ask outside [0.02; 0.98]
    assert [s["side"] for s in db.pm_sides(base, acc) if s["edge"] >= 0.05] == ["no"]
    assert not [s for s in db.pm_sides({**base, "no_ask_usd": 199.0}, acc) if s["side"] == "no"]
    assert not [s for s in db.pm_sides({**base, "no_ask": 0.985}, acc) if s["side"] == "no"]


def test_pm_edge_rr_and_size_math():
    _, rows = _pm_rows()
    acc = db.pm_accepted_hashes(rows)
    base = _pm_base(rows)
    s = next(x for x in db.pm_sides(base, acc) if x["side"] == "no")
    cost = 0.79 + 0.07 * 0.79 * 0.21  # ask + fee bound 0.07 * p (1 - p)
    assert s["ask"] == 0.79 and s["cost"] == pytest.approx(cost)
    assert s["rr"] == pytest.approx((1 - cost) / cost) and s["rr"] < 2
    assert s["shares"] == 124 and s["stake"] == pytest.approx(124 * cost) and s["payout"] == 124.0
    assert s["edge"] == pytest.approx(s["p_win"] - cost)
    # NO wins with 1 - the larger YES reading: the less favourable of k = 1 and k = 0.92
    assert s["p_win"] == pytest.approx(min(s["p_k1"], s["p_k092"])) and s["p_k1"] == pytest.approx(1 - base["model_prob"])
    # a fee-free cheap side: 0.25 -> R:R 3, 400 shares for $100, payout $400
    row = {**base, "fee_rate": 0.0, "yes_ask": 0.25, "yes_ask_usd": 500.0}
    y = next(x for x in db.pm_sides(row, acc) if x["side"] == "yes")
    assert (y["cost"], y["rr"], y["shares"], y["stake"], y["payout"]) == (0.25, 3.0, 400, 100.0, 400.0)


def test_pm_tab_renders_the_real_snapshot(tmp_path):
    from dataclasses import replace

    from tradedesk.analytics import pm_gap_paper as P

    cols, rows = _pm_rows()
    p = _pm_db(tmp_path / "s.sqlite", rows, cols)
    data, err = db.load_pm_gap(p)
    assert err is None and data["ts"] == PM_B and {r["ts"] for r in data["rows"]} == {PM_B}
    # the resolution texts are read from any row of the latest snapshot's markets
    latest = [r for r in rows if r["ts"] == PM_B]
    ids = {r["market_id"] for r in latest}
    acc = P.accepted_rule_hashes(rows)
    assert db.pm_accepted_hashes(data["text_rows"] + data["rows"]) == P.accepted_rule_hashes([r for r in rows if r["market_id"] in ids])
    now = float(PM_B + 600)
    v = db.pm_gap_view(data, now)
    ref_pass = sorted((s for r in latest for s in P.entry_signals(r, P.EntryRule(), acc)), key=lambda s: -s["edge"])
    ref_near = [s for r in latest for s in P.entry_signals(r, replace(P.EntryRule(), threshold=0.03), acc) if s["edge"] < 0.05]
    assert [(s["market_id"], s["side"]) for s in v["pass"]] == [(s["market_id"], s["side"]) for s in ref_pass]
    assert [(s["market_id"], s["side"]) for s in v["pass"]] == [("5690024", "no"), ("5690001", "no")]
    assert len(v["near"]) == len(ref_near)
    assert v["n_rows"] == len(latest) and v["n_eligible"] == sum(P.eligible(r, P.EntryRule(), acc) for r in latest)
    page = db.render_polymarket(data, None, now, p)
    for text in (
        "aria-current=page>Polymarket",
        "Проходять поріг (≥ 5 п.п.)",
        "href='https://polymarket.com/event/bitcoin-above-on-october-21-2026'",
        "Will the price of Bitcoin be above $84,000 on October 21?",
        "<b>NO</b>",
        "<td class='num'>0.790</td>",
        "124 шт · ставка $99.40",
        "виплата $124",
        "нижче правила 5 власника",
        "R:R нижче 2 (правило 5 власника): 2 з 2.",
        f"Близько (3–5 п.п.) — нижче порогу реєстрації, не сигнал: {len(ref_near)}",
        "ризик-нейтральні",
        "≈ 23.02.2027",
        "власні угоди на тих самих тонких ринках можуть зсунути книги",
        "правило 2 (сумарний ризик ≤ $300) і в денний стоп правила 4",
        # a workable record: journal_add with the market id (plan_review scores exchange candles only)
        "journal_add: symbol PM:&lt;market_id&gt;:&lt;YES|NO&gt;",
        "stop 0.001, теги idea:polymarket-vs-options-forward і pm:&lt;market_id&gt;",
        "journal_close з exit 1 (виграш) або 0.001 (програш)",
        "plan_add тут не підходить",
        "<code>PM:5690024:NO</code>",
        "pm:5690024</span>",
        "не перевірено: колектор ще не пише book_ts",
        "10 хв тому",
        "4.5 год",  # 16 769 s - 600 s to the end
    ):
        assert text in page, text
    assert page.index("bitcoin-above") < page.index("ethereum-above")  # sorted by net edge
    assert "старший за 2 год" not in page and "<script" not in page and "це не сигнали" not in page
    assert "на тест вона не впливає" not in page and "через plan_add з тегом" not in page
    snap, asof = _snap()
    assert "href='/polymarket'" in db.render_brief(snap, None, asof)  # the tab is in the shared nav


def test_pm_tab_near_table_on_the_earlier_snapshot(tmp_path):
    cols, rows = _pm_rows()
    a = 1792563645  # the earlier snapshot: nothing passes 5 pp, two sides are at 3-5 pp
    data, err = db.load_pm_gap(_pm_db(tmp_path / "a.sqlite", [r for r in rows if r["ts"] <= a], cols))
    assert err is None and data["ts"] == a
    v = db.pm_gap_view(data, float(a + 600))
    assert not v["pass"] and [(s["market_id"], s["side"]) for s in v["near"]] == [("6002443", "yes"), ("5690024", "no")]
    yes = v["near"][0]
    assert yes["rr"] >= 2 and v["near"][1]["rr"] < 2
    page = db.render_polymarket(data, None, float(a + 600))
    assert (
        "Зараз жоден бік не проходить поріг 5 п.п." in page
        and f"нижче порогу реєстрації, не сигнал: 2{db._info('pm_near')}</summary>" in page
    )
    assert "Will Ethereum dip to $2,350 on October 21?" in page and "<b>YES</b>" in page
    assert page.count("нижче правила 5 власника") == 1  # only the expensive NO side is flagged


def test_pm_tab_with_the_sync_columns_stale_and_ended(tmp_path):
    cols, rows = _pm_rows()
    for r in rows:
        r.update(book_ts=r["ts"] + 5, surface_ts=r["ts"] + 40, model_forward=None)
    rows.append({**_pm_base(rows, "5690001"), "market_id": "skewed", "surface_ts": PM_B + 70})
    p = _pm_db(tmp_path / "s.sqlite", rows, cols, extra_cols=("book_ts", "surface_ts", "model_forward"), upto=None)
    data, err = db.load_pm_gap(p)
    assert err is None and data["rows"][0]["book_ts"] is not None
    v = db.pm_gap_view(data, float(PM_B + 600))
    assert [s["market_id"] for s in v["pass"]] == ["5690024", "5690001"]  # the copy read 65 s apart is left out
    page = db.render_polymarket(data, None, float(PM_B + 600))
    assert "не перевірено" not in page and "<code>PM:5690024:NO</code>" in page and "це не сигнали" not in page
    # a snapshot older than 2 h: the rows stay, collapsed and labelled as stale, not as signals
    stale = db.render_polymarket(data, None, float(PM_B + 2 * 3600 + 60))
    assert "старший за 2 год" in stale
    assert "<details class='near'><summary>Ціни застарілі (знімок старший за 2 год): це не сигнали · рядків 2</summary>" in stale
    assert f"не сигнал · ціни застарілі: 0{db._info('pm_near')}</summary>" in stale
    # the horizon is also checked from NOW: 1.5 h before the end both passing sides are dropped and counted
    for now in (PM_END - 1.5 * 3600, PM_END - 2 * 3600 + 1, float(PM_END)):
        v = db.pm_gap_view(data, now)
        assert not v["pass"] and v["n_short"] >= 2, now
    page = db.render_polymarket(data, None, PM_END - 1.5 * 3600)
    assert "ближче ніж за 2 год до кінця (правило там уже не входить), не показано: 2" in page
    assert "<code>PM:5690024:NO</code>" not in page
    assert len(db.pm_gap_view(data, PM_END - 2 * 3600)["pass"]) == 2  # exactly 2 h left: still in


def test_pm_hours_left_format():
    assert [db._hours_left(h) for h in (-0.1, 0.4, 0.6, 1.5, 9.96, 30.2, 72.0)] == [
        "0 хв", "24 хв", "36 хв", "1.5 год", "10.0 год", "30 год", "3.0 дн",
    ]  # fmt: skip


def test_pm_tab_missing_db_table_and_empty_snapshot(tmp_path):
    none = tmp_path / "none.sqlite"
    data, err = db.load_pm_gap(none)
    assert data is None and err == "missing" and not none.exists()  # read-only: nothing is created
    page = db.render_polymarket(data, err, float(PM_B), none)
    assert "Бази ще немає" in page and "aria-current=page>Polymarket" in page and "ризик-нейтральні" in page
    bare = tmp_path / "bare.sqlite"
    con = sqlite3.connect(bare)
    con.execute("CREATE TABLE funding_hist (ts INTEGER)")
    con.close()
    data, err = db.load_pm_gap(bare)
    assert err is None and data["rows"] is None
    assert "Таблиці pm_gap_snapshot у базі ще немає" in db.render_polymarket(data, err, float(PM_B))
    cols, rows = _pm_rows()
    data, err = db.load_pm_gap(_pm_db(tmp_path / "empty.sqlite", [], cols))
    assert err is None and data == {"ts": None, "rows": [], "text_rows": []}
    assert "Знімків ще немає" in db.render_polymarket(data, err, float(PM_B))
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all" * 100)
    data, err = db.load_pm_gap(junk)
    assert data is None and err.startswith("unreadable")
    assert "Базу не вдалося прочитати" in db.render_polymarket(data, err, float(PM_B))
    # an odd row is skipped and counted, the page still renders
    bad = {"ts": PM_B, "rows": [{**_pm_base(rows), "end_ts": "x"}], "text_rows": rows}
    assert db.pm_gap_view(bad, float(PM_B))["n_bad"] == 1
    assert "несподіваним форматом пропущено: 1" in db.render_polymarket(bad, None, float(PM_B))


def test_pm_tab_page_reads_the_store_read_only(tmp_path, monkeypatch):
    cols, rows = _pm_rows()
    p = _pm_db(tmp_path / "s.sqlite", rows, cols)
    seen = {}
    real_connect = sqlite3.connect

    def spy(database, *a, **k):
        con = real_connect(database, *a, **k)
        seen.update(uri=database, kw=k)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM pm_gap_snapshot")
        return con

    monkeypatch.setattr(db.sqlite3, "connect", spy)
    monkeypatch.setattr(db, "PM_DB", p)
    page = db.polymarket_page(now=float(PM_B + 60))
    assert seen["uri"].endswith("?mode=ro") and seen["kw"].get("uri") is True
    assert "Проходять поріг" in page and "bitcoin-above-on-october-21-2026" in page


def test_pm_tab_escapes_question_and_slug(tmp_path):
    cols, rows = _pm_rows()
    for r in rows:
        if r["market_id"] == "5690024":
            r["question"] = "<script>alert(1)</script> above?"
            r["event_slug"] = "x' onmouseover='alert(2)"
    p = _pm_db(tmp_path / "s.sqlite", rows, cols)
    page = db.render_polymarket(*db.load_pm_gap(p), float(PM_B + 600), path=tmp_path / "<i>.sqlite")
    for raw in ("<script>alert", "onmouseover='alert", "<i>.sqlite"):
        assert raw not in page, raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt; above?" in page
    assert "href='https://polymarket.com/event/x%27%20onmouseover%3D%27alert%282%29'" in page


# ---------------------------------------------------------------- help icons and trade-lean highlights


def test_help_keys_used_by_the_pages_all_exist():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "dashboard.py").read_text()
    import re

    used = set(re.findall(r"help=\"(\w+)\"", src)) | set(re.findall(r"_h2\([^)]*?, '(\w+)'", src))
    used |= set(re.findall(r"\"(\w+)\", block_\w+\)", src)) - {""}
    used |= set(re.findall(r"_info\('(\w+)'\)", src))
    missing = {k for k in used if k not in db.HELP}
    assert not missing, missing
    for title, what, read, trade in db.HELP.values():
        assert title and what and read and trade


def test_info_icon_is_escaped_static_text_and_unknown_key_is_empty():
    html_ = db._info("dvol")
    assert html_.startswith("<span class='info' tabindex='0' role='button'")
    assert "DVOL" in html_ and "Для угоди:" in html_
    assert db._info("no-such-key") == ""
    tile = db._tile("K", "1", "s", help="iv", tone="good", badge="a<b")
    assert "class='tile t-good'" in tile and "a&lt;b" in tile and "class='info'" in tile


def test_vol_lean_thresholds():
    assert db.vol_lean(0.1, None)[0][0] == "good"
    assert db.vol_lean(0.5, None)[0][0] == ""
    assert db.vol_lean(0.9, None)[0][0] == "bad"
    assert db.vol_lean(None, -0.01)[0][0] == "good"
    assert db.vol_lean(None, 0.05)[0][0] == ""
    assert db.vol_lean(None, 0.12)[0][0] == "bad"
    assert db.vol_lean(None, None) == []
    assert db._flip("good") == "bad" and db._flip("warn") == "warn"


def test_brief_has_help_icons_conditions_and_howto():
    snap, asof = _snap()
    page = db.render_brief(snap, None, asof + 300)
    assert page.count("class='info' tabindex='0'") >= 8
    assert "class='howto'" in page
    assert "Купувати чи продавати опціони зараз" in page
    assert "<h3>Купувати опціони</h3>" in page and "<h3>Продавати опціони</h3>" in page
    assert "class='pcsum'" in page
    # buyer and seller read the price of volatility with opposite tones
    buy, sell = db.market_lean(snap)
    assert [db._flip(t) for t, _ in buy[:1]] == [t for t, _ in sell[:1]]


def test_page_wrappers_add_the_tooltip_script(monkeypatch):
    monkeypatch.setattr(db, "SNAPSHOT_PATH", SNAP_FIXTURE)
    for page in (db.brief_page(), db.options_page("")):
        assert page.count("<script>") >= 1 and "querySelectorAll('.info')" in page
        assert page.endswith("</body></html>")
    assert db.with_help("<body>x</body>") == f"<body>x<script>{db.HELP_JS}</script></body>"


def _all_tab_pages(tmp_path) -> dict[str, str]:
    """Every tab rendered from real fixtures, including the empty states that have their own headings."""
    snap, asof = _snap()
    no_chain = json.loads(json.dumps(snap))
    no_chain["sections"].pop("chain")
    pages = {"project": db.render(_project_snap()), "brief": db.render_brief(snap, None, asof + 300)}
    for q in ("", "s=short_strangle", "s=iron_condor"):
        pages[f"options {q}"] = db.render_options(snap, None, asof + 300, db.parse_options_query(q))
    pages["options no chain"] = db.render_options(no_chain, None, asof + 300)
    rsnap, t = _radar_snap()
    for asset in ("BTC", "ETH"):
        pages[f"radar {asset}"] = db.render_radar(rsnap, None, t + 300, db.parse_radar_query(f"asset={asset}"))
    p = _journal_db(tmp_path / "j.sqlite", _sample_journal(), _sample_rec())
    con = sqlite3.connect(p)
    con.executemany(f"INSERT INTO trade_plan ({P_COLS}) VALUES ({','.join('?' * 15)})", _sample_plans())
    con.executemany(f"INSERT INTO forecast_log ({F_COLS}) VALUES ({','.join('?' * 19)})", _sample_forecasts())
    con.commit()
    con.close()
    pages["journal"] = db.render_journal(*db.load_journal(p), float(T0 + 10 * 86400))
    pages["journal empty"] = db.render_journal(*db.load_journal(_journal_db(tmp_path / "e.sqlite", [], [])), float(T0))
    cols, rows = _pm_rows()
    pages["polymarket"] = db.render_polymarket(*db.load_pm_gap(_pm_db(tmp_path / "pm.sqlite", rows, cols)), float(PM_B + 600))
    return pages


def test_every_heading_and_tile_on_every_tab_has_a_help_note(tmp_path):
    import re

    icon = "<span class='info' tabindex='0' role='button'"
    pages = _all_tab_pages(tmp_path)
    n_heads = n_tiles = 0
    for name, page in pages.items():
        heads = re.findall(r"<h2>(.*?)</h2>", page, re.S)
        assert heads, name
        for h in heads:  # _info() gives '' for a key missing from HELP, so this also checks the key exists
            assert icon in h, (name, h[:80])
        tiles = re.findall(r"<div class='tile[^']*'><span class='k'>(.*?)<span class='v'>", page, re.S)
        for k in tiles:
            assert icon in k, (name, k[:80])
        n_heads, n_tiles = n_heads + len(heads), n_tiles + len(tiles)
        assert "Знімок не вдалося показати" not in page and "Блок не вдалося показати" not in page, name
    # the headings asked for by the owner (22.10), each with its icon
    for name, title in (
        ("project", "Черга"),
        ("project", "Питання агентів до тебе"),
        ("options ", "Позиція"),
        ("options no chain", "Конструктор"),
        ("journal", "Мої угоди"),
        ("journal", "Угоди"),
        ("journal empty", "Мої угоди"),
        ("journal", "Журнал прогнозів"),
        ("journal empty", "Журнал прогнозів"),
        ("polymarket", "Проходять поріг (≥ 5 п.п.)"),
    ):
        assert f"<h2>{title}{icon}" in pages[name], (name, title)
    assert "Чистий edge" + icon in pages["polymarket"] and "R:R" + icon in pages["polymarket"]
    assert n_heads >= 60 and n_tiles >= 40, (n_heads, n_tiles)  # the regexes really matched the pages
    # every note is static text from HELP: what it is, how to read it, what it means for a trade
    for key, (title, what, read, trade) in db.HELP.items():
        assert title and what and read and trade, key


def test_options_pros_and_cons_for_buyer_and_seller():
    snap, asof = _snap()
    page = db.render_options(snap, None, asof + 300, db.parse_options_query(""))
    assert "За і проти цієї угоди" in page and "class='pcsum'" in page
    assert "Для прибутку BTC має пройти щонайменше" in page
    assert "Час забирає" in page and "сплаченої премії щодня" in page
    sell = db.render_options(snap, None, asof + 300, db.parse_options_query("s=short_strangle"))
    assert "Збиток необмежений" in sell and "Час працює на тебе" in sell
    assert "class='tile t-bad'" in sell  # unbounded max loss


def test_position_pros_cons_risk_fit_and_required_move():
    fit_ok = {"unbounded": False, "fits": True, "loss": 80.0, "budget": 100.0, "qmax": 0.2}
    fit_big = {"unbounded": False, "fits": False, "loss": 300.0, "budget": 100.0, "qmax": 0.03}
    legs = [{"kind": "call", "side": "long", "strike": 100.0, "size": 1.0, "entry_premium": 5.0}]
    G = {"delta": 0.5, "gamma": 0.0, "vega": 1.0, "theta": -0.5}
    ext = {"max_loss": -5.0, "max_profit": None}
    near = db.position_pros_cons(legs, 100.0, 30 / 365, 0.5, -5.0, [105.0], G, ext, fit_ok, {})
    assert near[0][0] == "good" and near[1][0] == "good"  # 5 % needed vs 1σ ≈ 14 %
    assert near[2][0] == "bad" and "10.0 % сплаченої премії щодня" in near[2][1]
    far = db.position_pros_cons(legs, 100.0, 30 / 365, 0.5, -5.0, [130.0], G, ext, fit_big, {})
    assert far[0][0] == "bad" and "під нього — до 0.03 BTC" in far[0][1]
    assert far[1][0] == "bad"  # 30 % needed vs ≈ 14 %


# ---------------------------------------------------------------- «Радар» tab (the snapshot's radar section)

# archive rows of 11.10 and 18.10, daily DVOL and Binance closes, fetched 2026-10-22 from this server (see test_radar.py)
RADAR_INPUTS = Path(__file__).resolve().parent / "fixtures" / "radar_snapshot_inputs_2026-10-22.json"
AT_1810 = datetime(2026, 10, 18, 21, tzinfo=UTC)  # 18.10 is the newest due archive day


def _radar_section(at: datetime, keep: set[str] | None = None) -> dict:
    """The snapshot's `radar` section as `collector.assistant_snapshot.build_radar` writes it, from the real inputs;
    `keep` limits the archive rows to these days. JSON round trip, as the dashboard reads it from the file."""
    from tradedesk.analytics.macro_calendar import build_macro_calendar
    from tradedesk.analytics.radar import METHOD, radar_view
    from tradedesk.tools.macro import _load_seed

    d = json.loads(RADAR_INPUTS.read_text())
    ms = at.timestamp() * 1000
    events = build_macro_calendar([], _load_seed(), at, 400)["events"]
    assets = {}
    for asset in ("BTC", "ETH"):
        rows = [r for r in d["rows"][asset].values() if keep is None or r["day"] in keep]
        dvol = [tuple(x) for x in d["dvol"][asset] if x[0] <= ms]
        closes = [tuple(x) for x in d["closes"][asset] if x[0] <= ms]
        assets[asset] = radar_view(asset, rows, dvol, closes, events, at)
    sec = {"ok": True, "data": {"method": METHOD, "event_kinds": [], "assets": assets}, "missing": [], "warnings": []}
    return json.loads(json.dumps(sec))


def _radar_snap(at: datetime = AT_1810, keep: set[str] | None = None) -> tuple[dict, float]:
    snap, _ = _snap()
    snap["sections"]["radar"] = _radar_section(at, keep)
    snap["asof_utc"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return snap, at.timestamp()


def test_radar_tab_renders_the_real_section():
    snap, t = _radar_snap()
    v = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    assert v["missing"] == []
    page = db.render_radar(snap, None, t + 300, db.parse_radar_query("asset=BTC"))
    for text in (
        "aria-current=page>Радар",
        "href='/radar?asset=BTC' aria-current=page>BTC",
        "href='/radar?asset=ETH'>ETH",
        "1 · Ціна",
        "2 · ATM IV за строками",
        "3 · IV Rank за рік",
        "4 · IV проти реального руху",
        "5 · Скью 25Δ · 30 днів",
        "Усмішка на 30 днів",
        "6 · Форвардна вола між експіраціями і події",
        "81 093",  # the card's «Підсумки 18.10»
        "83 565",
        db._pc(v["price"]["change"] * 100),
        "<span class='chip ev'>гарячий тиждень</span> <b class='num'>30.10–06.11</b>: 42.1 %",  # the US midterms window
        "Не інвестиційна порада",
        "H1 відхилено",
        "5 хв тому",
    ):
        assert text in page, text
    for k in ("7", "30", "90", "180"):
        assert f"{v['atm']['now'][k] * 100:.1f} %" in page and f"{v['atm']['week'][k] * 100:.1f} %" in page, k
    assert db._dvp(v["atm"]["change"]["7"]) in page
    assert f"{v['iv_rank']['now']['rank'] * 100:.1f} %" in page and f"{v['iv_rank']['week']['rank'] * 100:.1f} %" in page
    assert db._lvp(v["skew"]["now"]["put_skew_25"]) in page and db._dvp(v["skew"]["change"]["put_skew_25"]) in page
    assert db._lvp(v["skew"]["now"]["rr_25"]).startswith("−")  # RR < 0: puts over calls
    far = [w for w in v["forward_vol"]["windows"] if w["to_days"] > db.RADAR_FWD_DAYS]
    assert (
        far and f"не показано, як у макеті: {len(far)}." in page and db._dd(far[0]["from_utc"]) + "–" not in page
    )  # the mock-up's horizon
    # the forward-vol chart: three one-day windows grey, the midterms week amber, the events as labelled lines
    assert page.count("fill:var(--line);stroke:var(--mut)") == 3 and page.count("fill:var(--warn);stroke:var(--warn)") == 1
    assert ">Вибори 03.11</text>" in page and ">FOMC 28.10</text>" in page and ">25.12</text>" in page
    assert db._vp(v["iv_vs_rv"]["rv30"]) in page and db._dvp(v["iv_vs_rv"]["premium_rv30"]) in page
    assert f"<td class='r num'>{v['smile']['now']['10P'] * 100:.1f}</td>" in page
    hot = [w for w in v["forward_vol"]["windows"] if w["hot"]]
    assert len(hot) == 1 and page.count("chip ev'>гарячий тиждень</span>") == 1
    assert [e["kind"] for e in hot[0]["events"]] == ["election"] and db._e(db._clean(hot[0]["events"][0]["title"], 60)) in page
    assert page.count("<svg") == 6  # year price, ATM term, IV Rank dial, year DVOL, smile, forward vol (skew history: 2 days)
    assert "Історії скью ще мало: 2 дн. в архіві поверхні" in page
    assert "Блок не вдалося" not in page and "немає даних" not in page and "<script" not in page
    eth = db.render_radar(snap, None, t + 300, db.parse_radar_query("asset=eth"))
    e = snap["sections"]["radar"]["data"]["assets"]["ETH"]
    assert "2 363" in eth and "href='/radar?asset=ETH' aria-current=page>ETH" in eth
    assert f"{e['atm']['now']['7'] * 100:.1f} %" in eth  # the card's front inversion: 52.0 %
    assert "href='/radar'" in db.render_brief(snap, None, t)  # the tab is in the shared nav


def _between(page: str, start: str, end: str) -> str:
    i = page.index(start)
    return page[i : page.index(end, i)]


def test_radar_tab_situation_and_deal_blocks_on_the_real_section():
    snap, t = _radar_snap()
    v = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    page = db.render_radar(snap, None, t + 300, db.parse_radar_query("asset=BTC"))
    # «Поточна ситуація» comes first, «Що це означає для угод» last
    assert page.index("Поточна ситуація") < page.index("1 · Ціна") < page.index("Що це означає для угод")
    now = _between(page, "Поточна ситуація", "</section>")
    assert now.count("<li>") == 5
    for text in (
        "<span class='chip calm'>ціна</span><span>BTC −3.0 % за тиждень: 81 093 проти 83 565 (18.10 проти 11.10, 19:00 UTC).",
        "<span class='chip ev'>вола</span><span>IV змінилась по-різному: 7 дн +1.9 вп, 30 дн +1.6 вп, 90 дн −0.3 вп",
        "структура висхідна: 7 днів нижче 30 на 2.9 вп.",
        f"<span class='chip opp'>IV Rank</span><span>IV Rank {v['iv_rank']['now']['rank'] * 100:.1f} % (тиждень тому "
        f"{v['iv_rank']['week']['rank'] * 100:.1f} %)",
        "річного діапазону DVOL 33.6–82.6 % (DVOL 18.10)",
        "<span class='chip risk'>скью</span><span>Put skew 2.2 вп → 2.6 вп, RR −2.4 вп → −3.7 вп: захист від падіння подорожчав",
        "<span class='chip ev'>події</span><span>Найдорожчий тиждень — 30.10–06.11: форвардна вола 42.1 % проти медіани 36.5 %; "
        "на нього припадає: US midterm elections 2026 (election day) 03.11",
    ):
        assert text in now, text
    deal = _between(page, "Що це означає для угод", "</section>")
    call = db._b76(v["price"]["now"], v["price"]["now"], 30 / 365, v["atm"]["now"]["30"], True)
    for text in (
        "<b>Опціони дешеві відносно року</b> (IV Rank 11 %), але це не сигнал купувати",
        "Куплена волатильність з хеджем у нас програвала (−1.18 % спота за угоду [−1.81; −0.48])",
        "опціони <b>не дешеві</b>: IV 30 днів 37.0 % проти реалізованої 31.6 % за 30 днів",
        "Ринок закладає підвищений рух на <b>30.10–06.11</b> (US midterm elections 2026 (election day))",
        "Захист від падіння подорожчав за тиждень (put skew +0.5 вп, RR −1.4 вп): пути дорожчають відносно ATM",
        "Правило макета, нами не перевірено.",
        f"коштує ≈ {db._usd(call)} за 1 BTC",
        f"тож $100 — це ≈ {db._nf(100 / call, 3)} BTC",
        "H1) і відхилено",
        "ризик-нейтральні",
        "не порада",
    ):
        assert text in deal, text
    assert "Фронт дорожчий за місяць" not in deal  # BTC's front is upward-sloping on 18.10
    assert "пут-спред дешевший за голий пут" not in deal and "+1.43" not in deal  # review 22.10, p. 1-2
    eth = db.render_radar(snap, None, t + 300, db.parse_radar_query("asset=ETH"))
    now = _between(eth, "Поточна ситуація", "</section>")
    assert "<span class='chip risk'>вола</span>" in now and "інверсія фронту: 7 днів дорожчі за 30" in now
    assert "Жоден тиждень до 100 днів не виділяється" in now and "ETH −6.6 % за тиждень" in now
    deal = _between(eth, "Що це означає для угод", "</section>")
    assert "Фронт дорожчий за місяць (7 днів 52.0 %" in deal and "Ринок закладає підвищений рух" not in deal
    assert "лот 0.01 BTC" not in deal and " ETH. Спред" in deal


def test_radar_deal_quotes_h2_only_in_the_tested_percentile_regime():
    """Review 22.10, p. 1: H2 tested the DVOL 365-day percentile ≥ 0.80, not IV Rank > 0.6."""
    snap, t = _radar_snap()
    now = snap["sections"]["radar"]["data"]["assets"]["BTC"]["iv_rank"]["now"]
    pages = {}
    for p in (0.75, 0.85, None):
        now["rank"], now["percentile"] = 0.7, p
        page = db.render_radar(snap, None, t + 300, db.parse_radar_query("asset=BTC"))
        pages[p] = _between(page, "Що це означає для угод", "</section>")
    hi = pages[0.85]
    assert "(IV Rank 70 %, перцентиль DVOL 85 % за рік)" in hi and "«перцентиль DVOL ≥ 80 % за рік»" in hi
    assert "+1.43 % спота [−0.13; +3.45], але це описово (n = 16" in hi and "+0.09 % [−0.49; +0.88]" in hi
    for p, shown in ((0.75, "75 %"), (None, "—")):
        d = pages[p]
        assert "+1.43" not in d and "n = 16" not in d, p
        assert "(IV Rank 70 %), але перевірений режим (перцентиль DVOL ≥ 80 % за рік) не настав" in d, p
        assert f"зараз перцентиль {shown}. Загалом продаж ATM-стреддлу" in d and "+0.09 % [−0.49; +0.88]" in d, p


def test_radar_situation_range_needs_both_ends_and_fwd_chart_survives_past_windows():
    """Review 22.10, p. 4-5: a null DVOL max drops the range; windows ending at or before the row's day draw nothing."""
    snap, _ = _radar_snap()
    a = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    a["iv_rank"]["now"]["max"] = None
    line = next(t for _, lab, t in db.radar_situation(a, "BTC") if lab == "IV Rank")
    assert "річного діапазону DVOL (DVOL 18.10)" in line and "–0.0 %" not in line
    assert db.fwd_vol_svg([{"fwd": 0.4, "to_days": 0.0, "days": 7.0}, {"fwd": 0.4, "to_days": -3.0, "days": 7.0}]) == ""


def test_radar_tab_situation_and_deal_blocks_with_null_fields():
    snap, t = _radar_snap(keep={"2026-10-18"})  # no row a week earlier
    page = db.render_radar(snap, None, t + 60)
    now = _between(page, "Поточна ситуація", "</section>")
    assert "BTC 81 093 (18.10, 19:00 UTC); порівняти з тижнем тому нема з чим." in now
    assert "IV змінилась" not in now and "Структура висхідна: 7 днів нижче 30" in now
    assert "Put skew — → 2.6 вп, RR — → −3.7 вп: порівняти з тижнем тому нема з чим." in now
    assert "Захист від падіння подорожчав" not in _between(page, "Що це означає для угод", "</section>")
    empty, t = _radar_snap(keep=set())  # the archive job is off: only DVOL-based fields
    page = db.render_radar(empty, None, t + 60)
    now = _between(page, "Поточна ситуація", "</section>")
    assert now.count("<li>") == 1 and "IV Rank" in now
    deal = _between(page, "Що це означає для угод", "</section>")
    assert "IV Rank" in deal and "правилом 8: ризик" not in deal and "Відносно реального руху" not in deal
    a = empty["sections"]["radar"]["data"]["assets"]["BTC"]
    for k in list(a):
        a[k] = None
    page = db.render_radar(empty, None, t + 60)
    assert "Поточну ситуацію скласти нема з чого" in page and "Даних для висновків у секції радару немає" in page
    assert db._e(db.RADAR_DEAL_NOTE) in page and "Блок не вдалося" not in page
    # no radar section: neither block is drawn
    snap, asof = _snap()
    page = db.render_radar(snap, None, asof + 60)
    assert "Секції «радар» у знімку ще немає" in page and "Поточна ситуація" not in page and "Що це означає для угод" not in page


def test_radar_tab_shows_each_blocks_own_week_dates():
    """Card review 22.10, p. 4: the surface compares archive rows, IV Rank daily DVOL closes; the dates differ."""
    at = datetime(2026, 10, 21, 19, 30, tzinfo=UTC)  # newest due day 20.10 -> the 18.10 row is «now»; DVOL runs to 21.10
    snap, t = _radar_snap(at)
    v = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    assert (v["surface"]["now_day"], v["surface"]["week_day"]) == ("2026-10-18", "2026-10-11")
    assert (v["iv_rank"]["now"]["day"], v["iv_rank"]["week"]["day"]) == ("2026-10-21", "2026-10-14")
    page = db.render_radar(snap, None, t + 60)
    assert "<b>18.10</b> 19:00 UTC проти <b>11.10</b>" in page and "(денний DVOL): <b>21.10</b> проти <b>14.10</b>" in page
    assert "DVOL 21.10: IV Rank" in page and "Тиждень тому (DVOL 14.10, пунктир)" in page
    assert "Зараз · 18.10" in page and "Тиждень тому · 11.10" in page and "18.10, 19:00 UTC, індекс Deribit" in page


def test_radar_tab_null_fields_show_their_reasons():
    snap, t = _radar_snap(keep={"2026-10-18"})  # no row a week earlier
    page = db.render_radar(snap, None, t + 60)
    assert "немає даних «поверхня тиждень тому»: no radar archive row for 2026-10-11" in page
    assert "81 093" in page and "Блок не вдалося" not in page and page.endswith("</body></html>")
    empty, t = _radar_snap(keep=set())  # the archive job is off
    page = db.render_radar(empty, None, t + 60)
    assert "Архіву поверхні BTC ще немає" in page and "немає даних «поверхня зараз»: no radar archive row from" in page
    assert "Ціни з архіву поверхні немає" in page and "Форвардної воли немає" in page
    assert page.count("<svg") == 3  # the IV Rank dial and the year DVOL and price charts do not need the archive
    assert "Історії скью ще немає" in page and "немає даних «скью за рік»: no radar archive skew from" in page
    sec = empty["sections"]["radar"]
    sec["missing"] = [
        {"field": "radar:ohlcv:BTC", "provider": "ccxt:binance", "reason": "error", "detail": "btc timeout"},
        {"field": "radar:ohlcv:ETH", "provider": "ccxt:binance", "reason": "error", "detail": "eth timeout"},
        {"field": "radar:events_seed", "provider": "tradedesk", "reason": "data_unavailable", "detail": "no seed"},
    ]
    sec["data"]["assets"]["BTC"]["iv_vs_rv"] = {"iv30": None, "rv30": None, "rv7": None}
    page = db.render_radar(empty, None, t + 60)
    assert "btc timeout" in page and "no seed" in page and "eth timeout" not in page
    assert "Ні IV, ні реалізованої волатильності в знімку немає" in page


def test_radar_tab_missing_section_snapshot_and_malformed(tmp_path, monkeypatch):
    snap, asof = _snap()  # the 20.10 fixture predates the radar section
    page = db.render_radar(snap, None, asof + 60)
    assert "Секції «радар» у знімку ще немає" in page and "aria-current=page>Радар" in page
    monkeypatch.setattr(db, "SNAPSHOT_PATH", tmp_path / "none.json")
    assert "Знімка асистента ще немає" in db.radar_page("asset=ETH", now=asof)
    monkeypatch.setattr(db, "SNAPSHOT_PATH", SNAP_FIXTURE)
    page = db.radar_page("")
    assert "querySelectorAll('.info')" in page and page.endswith("</body></html>")
    snap["sections"]["radar"] = {"ok": False, "data": None, "missing": [{"detail": "radar: KeyError: x"}], "warnings": []}
    page = db.render_radar(snap, None, asof + 60)
    assert "Даних BTC у секції радару немає" in page and "radar: KeyError: x" in page
    snap["sections"]["radar"] = {
        "ok": True,
        "data": {
            "assets": {
                "BTC": {
                    "surface": [],
                    "price": "x",
                    "atm": {"now": {"7": "a", "30": float("nan")}, "week": None},
                    "iv_rank": {"now": {"rank": 5}, "week": 3},
                    "iv_vs_rv": {"iv30": True, "rv30": -1},
                    "skew": {"now": [], "week": {"rr_25": "z"}},
                    "smile": {"now": {"ATM": 0.4}},
                    "forward_vol": {"windows": [1, None, {"fwd": "x", "events": [5, {"ts_utc": "bad"}]}], "median_weekly": "m"},
                    "missing": "z",
                }
            }
        },
        "missing": {"x": 1},
    }
    page = db.render_radar(snap, None, asof + 60)
    assert "1 · Ціна" in page and page.endswith("</body></html>") and "Знімок не вдалося показати" not in page
    assert "Поточна ситуація" in page and "Що це означає для угод" in page
    assert db.parse_radar_query("asset=eth") == {"asset": "ETH"}
    assert db.parse_radar_query("asset=%3Cx%3E") == {"asset": "BTC"} and db.parse_radar_query("") == {"asset": "BTC"}


def _full_year(asset: str = "BTC") -> dict:
    """`year_series` of a backfilled year: the real 11.10 / 18.10 archive rows alternating over 365 days, the real DVOL
    and Binance closes (JSON round trip, as the dashboard reads it)."""
    from tradedesk.analytics.radar import year_series

    d = json.loads(RADAR_INPUTS.read_text())
    src = [d["rows"][asset]["2026-10-11"], d["rows"][asset]["2026-10-18"]]
    end = datetime(2026, 10, 18, tzinfo=UTC)
    rows = [dict(src[(i + 1) % 2], day=(end - timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(365)]
    ms = AT_1810.timestamp() * 1000
    dvol = [tuple(x) for x in d["dvol"][asset] if x[0] <= ms]
    closes = [tuple(x) for x in d["closes"][asset] if x[0] <= ms]
    return json.loads(json.dumps(year_series(rows, dvol, closes, AT_1810, lambda f, r: None)))


def test_radar_year_charts_on_a_backfilled_year():
    snap, t = _radar_snap()
    a = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    a["year"] = _full_year()
    page = db.render_radar(snap, None, t + 60)
    price = _between(page, "1 · Ціна", "</section>")
    assert price.count("<svg") == 1 and 'aria-label="BTC: ціна за рік"' in price
    assert "Ціна за рік: денні закриття Binance BTC/USDT за днями UTC, 25.07.26–18.10.26, 86 з 365 днів" in price
    assert ">80k</text>" in price and ">сер</text>" in price  # price ticks and month labels
    rank = _between(page, "3 · IV Rank за рік", "</section>")
    assert rank.count("<svg") == 2 and 'aria-label="DVOL за рік"' in rank  # the dial and the year chart
    assert "DVOL за рік, 19.10.25–18.10.26: зараз 38.8 %, діапазон 33.6 % … 82.6 %" in rank and "з 365 днів" not in rank
    assert ">мін 33.6 %</text>" in rank and ">макс 82.6 %</text>" in rank and ">Rank 20 %</text>" in rank
    hist = _between(page, "Скью за рік", "</section>")
    assert hist.count("<svg") == 1 and hist.count("<path") == 2 and "медіана за 7 днів, вп" in hist
    assert "Архів поверхні 19.10.25–18.10.26, строк 30 днів" in hist and "з 365 днів" not in hist
    assert 'stroke:var(--mut)"/><text' in hist  # the zero line is drawn: RR is below zero
    assert page.count("<svg") == 7 and "Блок не вдалося" not in page and "немає даних" not in page


def test_radar_rolling_median_and_year_grid():
    N = None
    # from day 7 the 7-day window holds only 3.0, 2.0 (then 2.0, 9.0): fewer than 3 values
    assert db.rolling_median([1.0, N, 3.0, 2.0, N, N, N, N, N, 9.0], w=7, min_n=3) == [N, N, N, 2.0, 2.0, 2.0, 2.0, N, N, N]
    assert db.rolling_median([1.0, 4.0, 2.0, 8.0], w=3, min_n=2) == [None, 2.5, 2.0, 4.0]
    assert db._year_grid({"start": "2026-02-30", "values": [1]}, "values") is None  # not a date
    assert db._year_grid({"start": "2026-10-01", "values": "x"}, "values") is None
    start, (vals,) = db._year_grid({"start": "2026-10-01", "values": [1, "a", None, True, float("nan")] * 100}, "values")
    assert start.day == 1 and len(vals) == db.RADAR_YEAR_MAX_DAYS and vals[:5] == [1.0, None, None, None, None]
    assert db.year_svg(start, [([None, 1.0], "")], "x", db._vol_tick) == ""  # one point: no chart


def test_radar_year_charts_null_short_and_malformed():
    snap, t = _radar_snap()
    a = snap["sections"]["radar"]["data"]["assets"]["BTC"]
    a["year"] = None  # a snapshot from before this slice: blocks render as before
    page = db.render_radar(snap, None, t + 60)
    assert page.count("<svg") == 4 and "Історії скью ще немає" in page and "Блок не вдалося" not in page
    a["year"] = {
        "price": {"start": "2026-10-17", "values": [80000.0, None]},  # one point
        "dvol": {"start": "<b>x</b>", "values": [0.4, 0.5]},  # malformed start: no chart
        "skew": {"start": "2026-10-01", "put_skew_25": [0.02] * 25, "rr_25": "z"},
    }
    a["missing"] = [{"field": "year_dvol", "reason": "<script>x</script>"}]
    page = db.render_radar(snap, None, t + 60)
    assert page.count("<svg") == 4 and "Історії скью ще немає" in page and "Блок не вдалося" not in page
    assert "немає даних «DVOL за рік»: &lt;script&gt;x&lt;/script&gt;" in page and "<script" not in page and "<b>x" not in page
    a["year"]["skew"] = {"start": "2026-10-01", "put_skew_25": [0.02] * 10 + [None] * 20, "rr_25": [None] * 30}
    page = db.render_radar(snap, None, t + 60)
    assert "Історії скью ще мало: 10 дн. в архіві поверхні; графік — від 20 днів" in page


def test_radar_tab_escapes_hostile_strings_from_the_snapshot():
    snap, t = _radar_snap()
    sec = snap["sections"]["radar"]
    a = sec["data"]["assets"]["BTC"]
    hot = next(w for w in a["forward_vol"]["windows"] if w["hot"])
    hot["events"][0]["title"] = "<script>alert(1)</script>"
    a["forward_vol"]["windows"][0].update(fwd=None, reason="<img src=x onerror=alert(2)>", from_utc="<b>x</b>")
    a["missing"] = [{"field": "rv7", "reason": "<iframe src=//x.example>"}, {"field": "<svg onload=alert(3)>", "reason": "r"}]
    a["surface"]["week_day"] = "<u>w</u>"
    a["iv_rank"]["now"]["day"] = "<i>d</i>"
    sec["missing"] = [{"field": "radar:dvol:BTC", "detail": "<b>evil</b>"}]
    page = db.render_radar(snap, None, t + 60)
    for raw in ("<script", "<img", "<iframe", "<b>evil", "<b>x", "<u>w", "<i>d", "<svg onload"):
        assert raw not in page, raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;img src=x" in page and "&lt;iframe" in page
    assert "&lt;b&gt;evil" in page
    # the same title in «Поточна ситуація», «Що це означає для угод» and the chart's event notes, escaped each time
    assert page.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 3 and ">Вибори 03.11</text>" in page
    hot["events"][0]["kind"] = "<x>"  # an unknown kind: the chart labels it with the (escaped) title
    page = db.render_radar(snap, None, t + 60)
    assert "&lt;script&gt;alert( 03.11</text>" in page and "<script" not in page  # cut to 14 characters


# ---------------------------------------------------------------- the owner's rules (stdlib copies of analytics.rules_check)


def _rules_rows() -> list[dict]:
    """Journal rows shaped as the imports and journal_add write them (tests/test_rules_check.py, by hand)."""
    import test_rules_check as trc

    rows = trc._day_stop_rows() + trc._option_rows()
    rows += [
        {**r, "id": 100 + r["id"], "opened_ts": r["opened_ts"] + 7 * 86400, "closed_ts": r["closed_ts"] and r["closed_ts"] + 7 * 86400}
        for r in trc._alt_rows()
    ]
    rows += [
        trc._t(201, "BTCUSDT", "long", trc.D + 30 * H, trc.D + 31 * H, 20.0, "[]", size=0.01, entry=80000.0, stop=79000.0, target=82000.0),
        trc._t(202, "BTCUSDT", "long", trc.D + 32 * H, trc.D + 33 * H, 50.0, "[]", size=0.2, entry=80000.0, stop=79000.0, target=81000.0),
    ]
    return rows


def _same(mine, ref) -> None:
    """Exact on everything but floats (approx): the copies do the same arithmetic in the same order."""
    if isinstance(ref, dict):
        assert isinstance(mine, dict) and mine.keys() == ref.keys()
        for k in ref:
            _same(mine[k], ref[k])
    elif isinstance(ref, list):
        assert isinstance(mine, list) and len(mine) == len(ref)
        for a, b in zip(mine, ref, strict=True):
            _same(a, b)
    elif isinstance(ref, float):
        assert mine == pytest.approx(ref, abs=1e-9)
    else:
        assert mine == ref


def test_rules_copies_match_the_analytics_reference():
    from tradedesk.analytics import rules_check as rc

    exp = datetime(2026, 11, 27, 8, tzinfo=UTC).timestamp()
    for strat in db.OPT_STRATEGIES:
        for qty in (0.01, 0.03, 0.1):
            _, legs = _legs(strat, qty)
            for dep in (10_000.0, 30_000.0, 0.0):
                for days in (37.46, 3.0, 0.5):
                    now = exp - days * 86400
                    _same(db.rules_pre_trade(legs, now, exp, dep), rc.pre_trade_option(legs, now_ts=now, expiry_ts=exp, deposit_usd=dep))
            _same(db.rules_pre_trade(legs, exp, None), rc.pre_trade_option(legs, now_ts=exp, expiry_ts=None))
    import test_rules_check as trc

    rows = _rules_rows()
    now = datetime(2025, 10, 25, tzinfo=UTC).timestamp()
    hip3 = [
        {**r, "id": 400 + r["id"], "opened_ts": r["opened_ts"] + 14 * 86400, "closed_ts": r["closed_ts"] + 14 * 86400}
        for r in trc._hip3_rows()
    ]
    for dep in (10_000.0, 5_000.0):
        for rs in (rows, rows + hip3):
            _same(db.rules_trade_checks(rs, now, dep), rc.trade_checks(rs, now_ts=now, deposit_usd=dep))
            _same(db.rules_weekly(rs, now, dep), rc.weekly_violations(rs, now_ts=now, deposit_usd=dep))
            _same(db.rules_day_stops(rs, dep), rc.day_stops(rs, deposit_usd=dep))
    assert len(db.rules_weekly(rows, now)) == 3 and db.rules_weekly([], now) == []
    assert db.rules_is_hip3("xyz:TSLA") and not db.rules_is_hip3("SOL/USDT:USDT")
    for sym in (
        "BTC-31OCT26-90000-C",
        "ETH-3OCT25-4000-P-USDT",
        "BTC-31FEB26-1-C",
        "SOL/USDT:USDT",
        "1000PEPEUSDT",
        "SOL-PERP",
        "HYPE",
        "xyz:TSLA",
        None,
    ):
        assert db.rules_option_expiry_ts(sym) == rc.option_expiry_ts(sym) and db.rules_base_asset(sym) == rc.base_asset(sym), sym


def test_options_tab_shows_the_owners_rules_for_the_structure():
    snap, asof = _snap()
    page = db.render_options(snap, None, asof + 300, db.parse_options_query(""))
    rules = _between(page, "Правила власника перед угодою", "</section>")
    # 0.1 BTC of the 25Δ strangle: $291 paid, also the max loss; 37.5 days to the 27.11 expiry
    for text in (
        "Ризик ≤ 1 % депозиту ($100)",
        "Макс. збиток на експірацію $291 — у 2.9 раза більше за ліміт.",
        "Премія ≤ 1 % депозиту ($100)",
        "Платиш $291 — у 2.9 раза більше за ліміт.",
        "Не купувати за ≤ 3 дні до експірації",
        "<span class='chip opp'>виконано</span></td><td>До експірації 37.5 дн.",
    ):
        assert text in rules, text
    assert rules.count("<span class='chip risk'>порушено</span>") == 2
    # 0.03 BTC fits; a $30 000 deposit lifts both limits to $300
    small = _between(db.render_options(snap, None, asof + 300, db.parse_options_query("qty=0.03")), "Правила власника", "</section>")
    assert "chip risk" not in small and small.count("chip opp") == 3 and "Платиш $87." in small
    assert "Премія ≤ 1 % депозиту ($300)" in db.render_options(snap, None, asof + 300, db.parse_options_query("dep=30000"))
    # a sale: unbounded loss breaks rule 1, rule 8 does not apply
    sale = _between(db.render_options(snap, None, asof + 300, db.parse_options_query("s=short_strangle")), "Правила власника", "</section>")
    assert "Збиток на експірацію необмежений" in sale and sale.count("<span class='chip calm'>не стосується</span>") == 2
    # three days before the expiry buying is against rule 8
    late = db.render_options(snap, None, datetime(2026, 11, 24, 8, tzinfo=UTC).timestamp(), db.parse_options_query("qty=0.01"))
    assert "<span class='chip risk'>порушено</span></td><td>До експірації 3.0 дн." in late


def _rules_db(path: Path, rows: list[dict]) -> Path:
    from tradedesk.store.schema import MIGRATIONS

    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        for sql in MIGRATIONS[v]:
            con.execute(sql)
    cols = ("opened_ts", "closed_ts", "symbol", "side", "size", "entry", "stop", "target", "tags", "status", "pnl")
    con.executemany(
        f"INSERT INTO journal ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})", [tuple(r[c] for c in cols) for r in rows]
    )
    con.commit()
    con.close()
    return path


def test_journal_tab_shows_weekly_rule_violations(tmp_path):
    import test_rules_check as trc

    rows = _rules_rows()
    # an airdrop and a paper row opened after the 10.10 stop: not real trades, not judged
    rows += [
        trc._t(301, "HYPE", "long", trc.D + 15 * H, trc.D + 16 * H, 500.0, '["hyperliquid", "import", "perp", "airdrop"]'),
        trc._t(302, "SOL", "long", trc.D + 15 * H, trc.D + 16 * H, -5.0, '["pp-paper", "carry"]'),
    ]
    now = datetime(2025, 10, 25, tzinfo=UTC).timestamp()
    page = db.render_journal(*db.load_journal(_rules_db(tmp_path / "r.sqlite", rows)), now)
    block = _between(page, "Порушення правил за тиждень", "</section>")
    assert block.index("2025-W42") < block.index("2025-W41") < block.index("2025-W40")  # newest first
    w42 = _between(block, "2025-W42", "</tr>")
    # the alt longs of _alt_rows a week later + the late 3-day option buy (−$9): ADA, WIF (rule 2), WIF, AVAX (rule 4)
    # and the option (rule 8) for sure; Bybit LINK (+$3) only on its close time: shown apart
    for text in ("<span class='bad'>2 з 7</span> <span class='mut'>· невідомо 1</span>", "макс. одночасно 4", "стоп-днів 1",
                 "<span class='bad'>2 з 10</span>", "+ наближено 1 (за часом закриття)", "пізніх 1 · дорогих 0",
                 "<td class='num'>4<br><span class='mut'>+ наближено 1</span></td>",
                 "<td class='num bad'>−$607<br><span class='mut'>наближені +$3</span></td>"):  # fmt: skip
        assert text in w42, text
    w41 = _between(block, "2025-W41", "</tr>")
    # 10.10 in closing order: +30, −50, +15, −20 (the 1.8-day option buy), −50 at 09:00 = 2 losses in a row: the day
    # stops at 09:00, so SOL short (−30, opened 09:30) and DOGE (+40) broke rule 4, Bybit XRP (−20) only on its close
    # time; the option broke rule 8; journal_add: risk $200 and R:R 1 (+50) broke rules 1 and 5. 11 trades, 4 of them
    # broke a rule for sure: −30+40−20+50; XRP apart
    for text in (
        "<td class='num'>11</td>",
        "<span class='bad'>1 з 4</span> <span class='mut'>· невідомо 7</span>",
        "<span class='ok'>0 з 2</span> <span class='mut'>· невідомо 1</span>",
        "<span class='bad'>2 з 10</span>",
        "<span class='bad'>1 з 2</span> <span class='mut'>· невідомо 6</span>",
        "<span class='bad'>1 з 1</span> <span class='mut'>· невідомо 1</span>",
        "<td class='num'>4<br><span class='mut'>+ наближено 1</span></td>",
        "<td class='num ok'>+$40<br><span class='mut'>наближені −$20</span></td>",
    ):
        assert text in w41, text
    assert "дорогих 1" in _between(block, "2025-W40", "</tr>")
    # the airdrop and the paper row are left out: 11 + 11 + 1 real trades; exact −607 + 40 + 30 (the $150 put, rules 1
    # and 8); approximate +3 − 20
    assert "За весь журнал: хоч одне правило точно порушили 9 угод з 23, їхній P&amp;L разом −$537." in block
    assert "Ще 2 угод — лише наближене порушення правила 4" in block and "їхній P&amp;L −$17;" in block
    assert "4 · денний стоп" in block
    # empty journal, no journal table
    p = _journal_db(tmp_path / "e.sqlite", [], [])
    assert "Угод ще немає: звіт порушень з&#x27;явиться з першою угодою." in db.render_journal(*db.load_journal(p), now)
    bare = tmp_path / "bare.sqlite"
    sqlite3.connect(bare).execute("CREATE TABLE funding_hist (ts INTEGER)").connection.close()
    assert "Таблиці journal у базі ще немає: перевіряти нема що." in db.render_journal(*db.load_journal(bare), now)
