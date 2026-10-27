"""«Позиції» tab: live Bybit positions, P&L, greeks and breakevens (scripts/dashboard.py) and its collector
(scripts/bybit_live.py). Network-free: the collector runs against a fake ccxt client."""

import json
import math
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
