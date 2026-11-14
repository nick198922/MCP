"""analytics.option_ideas and scripts/option_ideas_tg.py: structures at Bybit bid/ask, sizing, payoff, message."""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.analytics import option_ideas as oi

NOW = int(datetime(2026, 11, 14, 10, tzinfo=UTC).timestamp() * 1000)
EXP = int(datetime(2026, 11, 27, 8, tzinfo=UTC).timestamp() * 1000)
SPOT = 67_000.0


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _chain(iv: float = 0.40, spread: float = 0.05) -> list[dict]:
    """Bybit-shaped ticker rows priced with Black-76 (r = 0) at one flat vol, strikes 54k..80k every 1k."""
    t = (EXP - NOW) / 86_400_000 / 365
    rows = []
    for k in range(54_000, 81_000, 1_000):
        d1 = (math.log(SPOT / k) + iv * iv * t / 2) / (iv * math.sqrt(t))
        d2 = d1 - iv * math.sqrt(t)
        call = SPOT * _ncdf(d1) - k * _ncdf(d2)
        put = call - SPOT + k
        for typ, px, delta in (("C", call, _ncdf(d1)), ("P", put, _ncdf(d1) - 1)):
            rows.append(
                {
                    "symbol": f"BTC-27NOV26-{k}-{typ}-USDT",
                    "bid1Price": str(round(px * (1 - spread), 2)) if px > 5 else "0",
                    "ask1Price": str(round(px * (1 + spread), 2)),
                    "markPrice": str(px),
                    "markIv": str(iv),
                    "delta": str(delta),
                    "underlyingPrice": str(SPOT),
                }
            )
    return rows


QUOTES = oi.parse_bybit_tickers(_chain(), "BTC")


def test_parse_skips_other_coins_and_bad_rows():
    rows = _chain()[:4] + [{"symbol": "ETH-27NOV26-2000-C-USDT", "underlyingPrice": "1900"}, {"symbol": "junk"}, "x"]
    qs = oi.parse_bybit_tickers(rows, "BTC")
    assert len(qs) == 4 and all(q.expiry_ms == EXP for q in qs)
    assert {q.is_call for q in qs} == {True, False}


def test_fee_is_capped_at_7pct_of_price():
    assert oi.fee(67_000, 1_000) == pytest.approx(20.1)  # 0.03 % of the underlying
    assert oi.fee(67_000, 100) == pytest.approx(7.0)  # 7 % cap


def test_pick_expiry_needs_liquidity_and_band():
    assert oi.pick_expiry(QUOTES, NOW) == EXP
    assert oi.pick_expiry(QUOTES, NOW, lo=20) is None
    assert oi.pick_expiry(QUOTES, NOW, min_liquid=10_000) is None


def test_iron_condor_prices_at_bid_ask_with_fees():
    c = oi.iron_condor(QUOTES, EXP, NOW, "BTC")
    assert c is not None and c.kind == "iron_condor"
    sp, lp, sc, lc = c.legs
    assert (sp.action, sp.is_call, lp.action, sc.action, sc.is_call, lc.action) == ("sell", False, "buy", "sell", True, "buy")
    assert lp.strike < sp.strike < SPOT < sc.strike < lc.strike
    assert sp.strike - lp.strike == lc.strike - sc.strike == 2_000  # one wing width (3 % of 67k ≈ 2k)
    gross = sp.price + sc.price - lp.price - lc.price
    fees = sum(oi.fee(SPOT, p) for p in (sp.price, sc.price, lp.price, lc.price))
    assert c.net == pytest.approx(gross - fees) and c.max_profit == c.net
    assert c.max_loss == pytest.approx(2_000 - c.net)
    assert c.breakevens == [pytest.approx(sp.strike - c.net), pytest.approx(sc.strike + c.net)]
    assert 0.5 < c.prob_profit < 0.9
    assert abs(abs(sp.delta) - 0.15) < 0.05 and abs(sc.delta - 0.15) < 0.05


def test_condor_payoff_matches_max_profit_and_loss():
    d = oi.iron_condor(QUOTES, EXP, NOW, "BTC").to_dict()
    assert oi.payoff_at(d, SPOT) == pytest.approx(d["max_profit"])
    assert oi.payoff_at(d, 40_000) == pytest.approx(-d["max_loss"])
    assert oi.payoff_at(d, 99_000) == pytest.approx(-d["max_loss"])
    assert oi.payoff_at(d, d["breakevens"][0]) == pytest.approx(0, abs=1e-6)


def test_bear_put_spread():
    s = oi.debit_spread(QUOTES, EXP, NOW, "BTC", bearish=True)
    assert s is not None and s.kind == "put_debit_spread" and s.net < 0
    lg, sh = s.legs
    assert lg.action == "buy" and sh.action == "sell" and sh.strike < lg.strike < SPOT + 2_000
    assert s.max_loss == pytest.approx(-s.net)
    assert s.max_profit == pytest.approx(lg.strike - sh.strike + s.net)
    d = s.to_dict()
    assert oi.payoff_at(d, 1_000) == pytest.approx(s.max_profit)
    assert oi.payoff_at(d, 99_000) == pytest.approx(-s.max_loss)
    assert d["reward_to_risk"] == pytest.approx(s.max_profit / s.max_loss)
    assert 0 < s.prob_profit < 0.5


def test_bull_call_spread_mirrors():
    s = oi.debit_spread(QUOTES, EXP, NOW, "BTC", bearish=False)
    lg, sh = s.legs
    assert lg.is_call and sh.is_call and lg.strike < sh.strike
    assert oi.payoff_at(s.to_dict(), 200_000) == pytest.approx(s.max_profit)


def test_long_strangle():
    s = oi.long_strangle(QUOTES, EXP, NOW, "BTC")
    assert s.max_profit is None and s.reward_to_risk is None
    d = s.to_dict()
    assert oi.payoff_at(d, SPOT) == pytest.approx(-s.max_loss)
    assert oi.payoff_at(d, s.breakevens[1]) == pytest.approx(0, abs=1e-6)


def test_size_rounds_down_to_lot():
    c = oi.iron_condor(QUOTES, EXP, NOW, "BTC")
    q = oi.size(c, 100)
    assert q * c.max_loss <= 100 and (q + 0.01) * c.max_loss > 100
    c.max_loss = 50_000
    assert oi.size(c, 100) == 0.0


def test_realized_vol_and_trend():
    flat = [100.0 * (1.01 if i % 2 else 1.0) for i in range(40)]
    rv = oi.realized_vol(flat)
    assert rv == pytest.approx(math.log(1.01) * math.sqrt(365) * math.sqrt(31 / 30), rel=0.05)
    assert oi.realized_vol([1.0] * 5) is None
    down = [300 - i * 0.5 for i in range(260)]
    assert oi.trend(down) == "bear" and oi.trend(down[::-1]) == "bull" and oi.trend([1.0] * 30) == "neutral"


@pytest.fixture
def p3_sendable(monkeypatch):
    """P3 as it was before its ✗ verdict (14.11), to keep testing the trade path and the message layout."""
    old = oi.RULES["P3"]
    monkeypatch.setitem(oi.RULES, "P3", replace(old, status="не перевірено", evidence="не перевірено: доказів нема"))


def test_p3_is_x_since_its_backtest_and_is_not_sent():
    down = [80_000 - i * 40 for i in range(260)]
    ideas, ctx = oi.ideas_for("BTC", QUOTES, down, NOW)
    assert ctx["trend"] == "bear" and ctx["checks"]["P3"]["met"] is True and ideas == []
    assert oi.RULES["P3"].status == "✗" and not oi.RULES["P3"].sendable


def test_ideas_for_sends_trend_spread_but_never_the_x_rules(p3_sendable):
    down = [80_000 - i * 40 for i in range(260)]  # falling, tiny realized vol → IV ≫ RV (P1 would say condor: ✗)
    ideas, ctx = oi.ideas_for("BTC", QUOTES, down, NOW)
    assert ctx["trend"] == "bear" and [(i.kind, i.rule) for i in ideas] == [("put_debit_spread", "P3")]
    assert ctx["iv_rv"] > 1 and all(i.why and i.qty > 0 for i in ideas)
    wild = [60_000 * (1.06 if i % 2 else 1.0) for i in range(260)]  # RV ≫ IV (P4 would say strangle: ✗), no trend
    ideas, ctx = oi.ideas_for("BTC", QUOTES, wild, NOW)
    assert ideas == [] and ctx["trend"] == "neutral" and ctx["checks"]["P3"]["met"] is False
    ideas, ctx = oi.ideas_for("BTC", [], down, NOW)
    assert ideas == [] and "skip" in ctx


# --- playbook rules ---------------------------------------------------------------------------------------------

PLAYBOOK = Path(__file__).resolve().parent.parent / oi.PLAYBOOK


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")  # GitHub's heading anchors


def test_rules_table_mirrors_the_playbook():
    heads = {_slug(line[4:].strip()) for line in PLAYBOOK.read_text(encoding="utf-8").splitlines() if line.startswith("### P")}
    assert set(oi.RULES) == {f"P{i}" for i in range(1, 16)}
    for r in oi.RULES.values():
        assert r.anchor in heads, r.code
        assert r.link == f"{oi.PLAYBOOK}#{r.anchor}"
        assert r.status in (*oi.EVIDENCE_ORDER, "✗") and r.kind in ("trade", "context")
        assert r.evidence.startswith(r.status if r.status != "не перевірено" else "не перевірено") or r.code == "P7"
    # ✗ rules and the ones no MCP tool measures are never sent
    not_sent = {"P1", "P3", "P4", "P5", "P14", "P6", "P7", "P8", "P11", "P12", "P13"}
    assert {c for c, r in oi.RULES.items() if not r.sendable} == not_sent
    assert {c for c, r in oi.RULES.items() if r.sendable} == {"P2", "P9", "P10", "P15"}


def test_rank_ideas_drops_x_and_orders_by_evidence(p3_sendable):
    c = oi.iron_condor(QUOTES, EXP, NOW, "BTC")
    s = oi.debit_spread(QUOTES, EXP, NOW, "BTC", bearish=True)
    c.rule, s.rule = "P1", "P3"  # P1 is ✗
    assert oi.rank_ideas([c, s]) == [s]
    unknown = oi.debit_spread(QUOTES, EXP, NOW, "BTC", bearish=False)
    assert oi.rank_ideas([unknown]) == []  # no rule → not sent
    fake = {f"T{i}": oi.Rule(f"T{i}", "t", "c", "k", st, st, "a") for i, st in enumerate(("не перевірено", "◯", "✓", "≈"))}
    ideas = []
    for code in fake:
        x = oi.debit_spread(QUOTES, EXP, NOW, "BTC", bearish=True)
        x.rule = code
        ideas.append(x)
    old = dict(oi.RULES)
    try:
        oi.RULES.update(fake)
        ranked = oi.rank_ideas(ideas)
    finally:
        oi.RULES.clear()
        oi.RULES.update(old)
    assert [i.rule for i in ranked] == ["T2", "T3", "T1"]  # ✓, ≈, ◯; the 4th cut by the limit


def test_p2_dvol_percentile():
    assert oi.check_dvol_high({"current": {"dvol": 0.62, "dvol_percentile_365d": 0.85}}).met is True
    assert oi.check_dvol_high({"current": {"dvol": 0.41, "dvol_percentile_365d": 0.53}}).met is False
    assert oi.check_dvol_high({"current": {"dvol_percentile_365d": 0.8}}).met is True  # the threshold is inclusive
    assert oi.check_dvol_high(None).met is None and oi.check_dvol_high({"current": {}}).met is None


def test_p3_trend():
    down = [300 - i * 0.5 for i in range(260)]
    assert oi.check_trend(down).met is True and oi.check_trend(down[::-1]).met is True
    assert oi.check_trend([1.0] * 260).met is False


def test_p9_event_within_48h():
    cal = {"high_impact_within_48h": True, "events": [{"kind": "cpi", "hours_ahead": 20.0}, {"kind": "fomc", "hours_ahead": 200.0}]}
    chk = oi.check_event(cal)
    assert chk.met is True and "CPI через 20 год" in chk.detail
    assert oi.check_event({"high_impact_within_48h": False, "events": []}).met is False
    assert oi.check_event(None).met is None and oi.check_event({"events": []}).met is None


def _term(front_iv: float) -> dict:
    return {
        "expiries": [
            {"expiry": "15NOV26", "days": 0.9, "atm_iv": 0.50},
            {"expiry": "20NOV26", "days": 5.9, "atm_iv": front_iv},
            {"expiry": "27NOV26", "days": 12.9, "atm_iv": 0.40},
            {"expiry": "11DEC26", "days": 26.9, "atm_iv": 0.39},  # nearest 30 d
            {"expiry": "25DEC26", "days": 40.9, "atm_iv": 0.30},
        ]
    }


def test_p10_front_inversion_with_event_inside_the_front():
    cal = {"events": [{"kind": "cpi", "hours_ahead": 50.0}]}  # day 2.1: after the 1-day expiry, before the 6-day one
    chk = oi.check_front_inversion(_term(0.41), cal)  # front 20NOV vs 11DEC: +2.0 points
    assert chk.met is True and "20NOV26" in chk.detail and "11DEC26" in chk.detail
    assert oi.check_front_inversion(_term(0.40), cal).met is False  # +1.0 < +1.5
    assert oi.check_front_inversion(_term(0.45), {"events": [{"kind": "nfp", "hours_ahead": 50.0}]}).met is False  # not FOMC / CPI
    assert oi.check_front_inversion(_term(0.45), {"events": [{"kind": "fomc", "hours_ahead": 240.0}]}).met is False  # > 8 d
    assert oi.check_front_inversion(_term(0.45), {"events": [{"kind": "cpi", "hours_ahead": -1.0}]}).met is False  # released
    assert oi.check_front_inversion(None, cal).met is None and oi.check_front_inversion(_term(0.45), None).met is None


def test_month_expiry_iso_is_the_collector_format():
    assert oi.month_expiry_iso(_term(0.41)) == "2026-12-11T08:00:00+00:00"
    assert oi.month_expiry_iso({"expiries": [{"expiry": "20NOV26", "days": 5.9, "atm_iv": 0.4}]}) is None


def test_p15_skew_fear_and_turn():
    assert oi.check_skew_fear({"rr": {"min": -0.062, "latest": -0.050}}).met is True
    assert oi.check_skew_fear({"rr": {"min": -0.062, "latest": -0.058}}).met is False  # no turn yet
    assert oi.check_skew_fear({"rr": {"min": -0.040, "latest": -0.020}}).met is False  # not fearful enough
    assert oi.check_skew_fear(None).met is None


def test_context_rules_land_in_notes_not_ideas():
    flat = [60_000.0] * 260
    market = {
        "vrp": {"current": {"dvol": 0.7, "dvol_percentile_365d": 0.9}},
        "calendar": {"high_impact_within_48h": True, "events": [{"kind": "cpi", "hours_ahead": 50.0}]},
        "term": _term(0.45),
        "skew": {"rr": {"min": -0.07, "latest": -0.05}},
    }
    ideas, ctx = oi.ideas_for("BTC", QUOTES, flat, NOW, market=market)
    assert ideas == [] and ctx["notes"] == ["P2", "P9", "P10", "P15"]
    json.dumps(ctx)  # logged with the ideas
    ideas, ctx = oi.ideas_for("BTC", QUOTES, flat, NOW)
    assert ctx["notes"] == [] and ctx["checks"]["P2"]["met"] is None


_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("option_ideas_tg", _SCRIPTS / "option_ideas_tg.py")
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)


def test_message_lists_p3_as_not_sent():
    down = [80_000 - i * 40 for i in range(260)]
    text = tg.build_message([oi.ideas_for("BTC", QUOTES, down, NOW)], NOW / 1000, 100.0, "")
    assert "Правило: P3" not in text and "пут-спред" not in text.split("Не надсилаємо")[0]
    assert "P3 200-денний тренд → дебетовий спред" in text.split("Не надсилаємо (✗ у плейбуку)")[1]


def test_message_has_strikes_premium_rr_size_rule_and_caveat(tmp_path, p3_sendable):
    down = [80_000 - i * 40 for i in range(260)]
    market = {"calendar": {"high_impact_within_48h": True, "events": [{"kind": "cpi", "hours_ahead": 20.0}]}}
    per = [oi.ideas_for("BTC", QUOTES, down, NOW, market=market), ([], {"coin": "ETH", "skip": "TimeoutError"})]
    text = tg.build_message(per, NOW / 1000, 100.0, "Журнал прогнозів: Brier 0.278")
    assert "Опціонні ідеї" in text and "пут-спред" in text
    assert "прибуток : ризик" in text and "Розмір під $100" in text and "27.11" in text
    assert "ризик-нейтральні" in text and "Brier 0.278" in text and "ETH · тренд" in text
    assert "Правило: P3 200-денний тренд → дебетовий спред · докази: не перевірено" in text
    assert "CPI через 20 год" in text and "Правило: P9" in text and "(контекст, не угода)" in text
    assert "Не надсилаємо (✗ у плейбуку): P1" in text and "P4" in text and "options-playbook.md" in text
    assert "1. BTC · ведмежий пут-спред" in text and "залізний кондор" in text.split("Не надсилаємо")[1]  # only as not sent
    log = tmp_path / "ideas.jsonl"
    assert tg.log_ideas(per, NOW / 1000, log) == 1
    row = json.loads(log.read_text().splitlines()[0])
    assert row["status"] == "open" and row["coin"] == "BTC" and row["rule"] == "P3" and len(row["legs"]) == 2
    assert row["context"]["checks"]["P9"]["met"] is True
    assert oi.payoff_at(row, 1_000) == pytest.approx(row["max_profit"])


def test_market_wide_note_is_shown_once():
    flat = [60_000.0] * 260
    market = {"calendar": {"high_impact_within_48h": True, "events": [{"kind": "fomc", "hours_ahead": 5.0}]}}
    per = [oi.ideas_for(c, QUOTES, flat, NOW, market=market) for c in ("BTC", "ETH")]
    text = tg.build_message(per, NOW / 1000, 100.0)
    assert text.count("Правило: P9") == 1 and "ідей нема" in text


class _Res:
    def __init__(self, env: dict):
        self.structured_content, self.is_error, self.content = env, False, []


class _Client:
    """Stands in for mcp.Client: answers from ``envs`` by tool name, raises for the rest; records the calls."""

    def __init__(self, envs: dict):
        self.envs, self.calls = envs, []

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        if name not in self.envs:
            raise RuntimeError("provider down")
        return _Res(self.envs[name])


def test_market_for_reads_the_mcp_tools():
    import asyncio

    term = _term(0.41)
    c = _Client(
        {
            "deribit_vrp": {"ok": True, "data": {"current": {"dvol_percentile_365d": 0.9}}},
            "deribit_atm_term": {"ok": True, "data": term},
            "skew_percentile": {"ok": True, "data": {"rr": {"min": -0.07, "latest": -0.05}}},
        }
    )
    m = asyncio.run(tg.market_for(c, "ETH", {"events": []}))
    assert m["vrp"]["current"]["dvol_percentile_365d"] == 0.9 and m["term"] == term and m["skew"]["rr"]["min"] == -0.07
    skew_args = dict(c.calls)["skew_percentile"]
    assert skew_args["expiry"] == "2026-12-11T08:00:00+00:00" and skew_args["currency"] == "ETH" and skew_args["lookback_days"] == 3


def test_market_for_survives_failed_tools():
    import asyncio

    m = asyncio.run(tg.market_for(_Client({"deribit_vrp": {"ok": False, "data": None}}), "BTC", None))
    assert m == {"vrp": None, "term": None, "calendar": None, "skew": None}
    _, ctx = oi.ideas_for("BTC", QUOTES, [60_000.0] * 260, NOW, market=m)
    assert ctx["notes"] == [] and all(ctx["checks"][c]["met"] is None for c in ("P2", "P9", "P10", "P15"))


def test_message_without_ideas():
    text = tg.build_message([([], {"coin": "BTC", "skip": "x"}), ([], {"coin": "ETH", "skip": "x"})], NOW / 1000, 100.0)
    assert "ідей нема" in text


# --- settlement of the ideas journal (card item 3) --------------------------------------------------------------

AFTER = EXP + 3_600_000  # an hour after the 08:00 UTC expiry


def _leg(action: str, is_call: bool, strike: float, price: float = 1.0) -> dict:
    return {"action": action, "is_call": is_call, "strike": strike, "price": price, "delta": None}


def _rec(kind: str, legs: list[dict], net: float, max_loss: float, qty: float = 0.5, rule: str = "P3", **kw) -> dict:
    return {
        "ts": NOW // 1000,
        "status": "open",
        "coin": "BTC",
        "kind": kind,
        "expiry_ms": EXP,
        "legs": legs,
        "net": net,
        "max_loss": max_loss,
        "qty": qty,
        "rule": rule,
        **kw,
    }


CALL_SPREAD = _rec("call_debit_spread", [_leg("buy", True, 100), _leg("sell", True, 110)], net=-4, max_loss=4)
PUT_SPREAD = _rec("put_debit_spread", [_leg("buy", False, 100), _leg("sell", False, 90)], net=-3, max_loss=3)
CONDOR = _rec(
    "iron_condor",
    [_leg("sell", False, 90), _leg("buy", False, 85), _leg("sell", True, 110), _leg("buy", True, 115)],
    net=2,
    max_loss=3,
)


@pytest.mark.parametrize(
    ("rec", "price", "pnl"),
    [
        (CALL_SPREAD, 95, -4),  # below both strikes: the debit is lost
        (CALL_SPREAD, 100, -4),  # at the long strike
        (CALL_SPREAD, 104, 0),  # breakeven
        (CALL_SPREAD, 105, 1),
        (CALL_SPREAD, 110, 6),  # at the short strike: max profit
        (CALL_SPREAD, 130, 6),
        (PUT_SPREAD, 105, -3),
        (PUT_SPREAD, 100, -3),
        (PUT_SPREAD, 95, 2),
        (PUT_SPREAD, 90, 7),
        (PUT_SPREAD, 50, 7),
        (CONDOR, 100, 2),  # between the shorts: keep the credit
        (CONDOR, 90, 2),  # at the short put
        (CONDOR, 88, 0),  # lower breakeven
        (CONDOR, 85, -3),  # at the long put: max loss
        (CONDOR, 60, -3),
        (CONDOR, 110, 2),
        (CONDOR, 112, 0),
        (CONDOR, 115, -3),
        (CONDOR, 200, -3),
    ],
)
def test_settle_idea_usd_and_r(rec, price, pnl):
    s = oi.settle_idea(rec, price, AFTER)
    assert s["status"] == "settled" and s["delivery_price"] == price and s["settled_ms"] == AFTER
    assert s["pnl_per_unit"] == pytest.approx(pnl)
    assert s["pnl_usd"] == pytest.approx(pnl * 0.5)
    assert s["r"] == pytest.approx(pnl / rec["max_loss"])
    assert -1 - 1e-9 <= s["r"]  # never more than the risked max loss


def test_settle_idea_matches_payoff_of_a_real_condor():
    d = oi.iron_condor(QUOTES, EXP, NOW, "BTC").to_dict()
    s = oi.settle_idea(d, SPOT, AFTER)
    assert s["r"] == pytest.approx(d["max_profit"] / d["max_loss"]) and s["pnl_usd"] is None  # qty 0: not sized


def test_settle_idea_waits_for_expiry_and_rejects_bad_input():
    assert oi.settle_idea(CALL_SPREAD, 105, EXP - 1) is None  # a millisecond before expiry
    assert oi.settle_idea(CALL_SPREAD, 105, EXP) is not None  # at expiry
    for price in (None, 0, -5, float("nan"), float("inf"), "x"):
        assert oi.settle_idea(CALL_SPREAD, price, AFTER) is None
    bad = [
        None,
        "x",
        {**CALL_SPREAD, "legs": []},
        {**CALL_SPREAD, "legs": None},
        {**CALL_SPREAD, "legs": [_leg("hold", True, 100)]},
        {**CALL_SPREAD, "legs": [{"action": "buy", "is_call": True}]},
        {**CALL_SPREAD, "max_loss": 0},
        {**CALL_SPREAD, "net": "?"},
        {k: v for k, v in CALL_SPREAD.items() if k != "expiry_ms"},
    ]
    for rec in bad:
        assert oi.settle_idea(rec, 105, AFTER) is None


def test_bybit_option_symbol_and_delivery_parse():
    assert oi.bybit_option_symbol("BTC", EXP, 70_000.0, True) == "BTC-27NOV26-70000-C-USDT"
    seven = int(datetime(2026, 11, 7, 8, tzinfo=UTC).timestamp() * 1000)
    assert oi.bybit_option_symbol("ETH", seven, 2512.5, False) == "ETH-7NOV26-2512.5-P-USDT"  # day not zero-padded
    # the shape of a live answer (14.11, BTC-13NOV26-70000-C-USDT)
    body = {
        "retCode": 0,
        "result": {"list": [{"symbol": "BTC-27NOV26-70000-C-USDT", "deliveryPrice": "68776.89740864", "deliveryTime": str(EXP)}]},
    }
    assert oi.parse_bybit_delivery(body, EXP) == pytest.approx(68776.89740864)
    assert oi.parse_bybit_delivery(body, EXP + 86_400_000) is None  # another expiry's price is never used
    for b in (None, {}, {"result": {"list": []}}, {"result": {"list": [{"deliveryPrice": "0"}, "x"]}}):
        assert oi.parse_bybit_delivery(b, EXP) is None


WEEK = 7 * 86_400_000


def _settled(r: float, rule: str = "P3", k: int | None = None) -> dict:
    """A settled record; ``k`` picks its expiry (EXP + k weeks), None leaves it without one."""
    rec = {"status": "settled", "rule": rule, "r": r}
    return rec if k is None else {**rec, "expiry_ms": EXP + k * WEEK}


def test_rule_summary_needs_30_for_a_verdict():
    rs = [0.5] * 20 + [-1.0] * 9
    recs = [_settled(r, k=i) for i, r in enumerate(rs)] + [_rec("x", [], 0, 1), "junk", {"status": "settled", "r": None}]
    s = oi.rule_summary(recs)
    assert list(s) == ["P3"]
    p3 = s["P3"]
    assert p3["n"] == 29 and p3["sum_r"] == pytest.approx(1.0) and p3["win_share"] == pytest.approx(20 / 29)
    assert p3["verdict"] is None and p3["se_r"] is None and p3["cluster_mean_r"] is None and p3["n_clusters"] == 29
    s = oi.rule_summary(recs + [_settled(0.5, k=29)])
    assert s["P3"]["n"] == 30 and s["P3"]["verdict"] == "flat"  # mean +0.05, 2 SE ≈ 0.26
    assert oi.rule_summary([_settled(0.5 + 0.01 * i, k=i) for i in range(30)])["P3"]["verdict"] == "plus"
    assert oi.rule_summary([_settled(-1.0 + 0.01 * i, k=i) for i in range(30)])["P3"]["verdict"] == "minus"
    mixed = oi.rule_summary([_settled(1.0, "—"), _settled(1.0, "P2"), {"status": "settled", "r": -1}])
    assert list(mixed) == ["P2", "—"] and mixed["—"]["n"] == 2  # RULES order; no rule → '—'
    assert oi.rule_summary(None) == {} and oi.rule_summary([]) == {}


def test_rule_summary_needs_10_expiries_for_a_verdict():
    strong = [0.5 + 0.01 * i for i in range(30)]  # 'plus' when independent
    nine = oi.rule_summary([_settled(r, k=i % 9) for i, r in enumerate(strong)])["P3"]
    assert nine["n"] == 30 and nine["n_clusters"] == 9 and nine["verdict"] is None and nine["se_r"] is None
    ten = oi.rule_summary([_settled(r, k=i % 10) for i, r in enumerate(strong)])["P3"]
    assert ten["n_clusters"] == 10 and ten["verdict"] == "plus"
    no_expiry = oi.rule_summary([_settled(r) for r in strong])["P3"]  # records without an expiry: one cluster
    assert no_expiry["n_clusters"] == 1 and no_expiry["verdict"] is None


def test_rule_summary_se_is_per_expiry_cluster():
    """30 ideas in 15 expiries with the same R inside each pair (BTC and ETH of one Saturday) carry the information
    of 15, not 30: the band is wider (√(870/420) ≈ 1.44×) and here turns a naive 'plus' into 'flat'."""
    vals = [0.2 + 0.1 * (k - 7) for k in range(15)]
    paired = oi.rule_summary([_settled(v, k=k) for k, v in enumerate(vals) for _ in range(2)])["P3"]
    indep = oi.rule_summary([_settled(v, k=2 * k + j) for k, v in enumerate(vals) for j in range(2)])["P3"]
    assert paired["n"] == indep["n"] == 30 and paired["n_clusters"] == 15 and indep["n_clusters"] == 30
    assert paired["mean_r"] == pytest.approx(indep["mean_r"]) and paired["cluster_mean_r"] == pytest.approx(0.2)
    assert paired["se_r"] / indep["se_r"] == pytest.approx(math.sqrt(870 / 420))
    assert indep["verdict"] == "plus" and paired["verdict"] == "flat"
    # unequal clusters: the verdict's mean is the mean of the per-expiry means, not of the ideas
    uneven = [_settled(1.0, k=0)] * 21 + [_settled(-0.1, k=k) for k in range(1, 10)]
    u = oi.rule_summary(uneven)["P3"]
    assert u["mean_r"] == pytest.approx((21 - 0.9) / 30) and u["cluster_mean_r"] == pytest.approx((1.0 - 0.9) / 10)


def _rows(*recs) -> list:
    return [(json.dumps(r) if isinstance(r, dict) else r, r if isinstance(r, dict) else None) for r in recs]


def test_due_lists_only_open_expired_records():
    later = {**CALL_SPREAD, "expiry_ms": EXP + 7 * 86_400_000}
    settled = {**PUT_SPREAD, "status": "settled"}
    rows = _rows(CALL_SPREAD, later, settled, "not json", {**CONDOR, "legs": 5}, {**CONDOR, "expiry_ms": 1e300})
    assert tg.due(rows, AFTER) == {("BTC", EXP): ["BTC-27NOV26-100-C-USDT", "BTC-27NOV26-110-C-USDT"]}
    assert tg.due(rows, EXP - 1) == {}


def test_settle_rows_is_idempotent_and_keeps_bad_lines(tmp_path):
    log = tmp_path / "ideas.jsonl"
    log.write_text(json.dumps(CALL_SPREAD) + "\n{broken\n\n" + json.dumps({**PUT_SPREAD, "coin": "ETH"}) + "\n[1, 2]\n", encoding="utf-8")
    rows = tg.read_log(log)
    assert len(rows) == 4 and rows[1] == ("{broken", None) and rows[3][1] is None
    prices = {("BTC", EXP): 105.0}  # ETH: no delivery price → stays open
    rows, n = tg.settle_rows(rows, prices, AFTER)
    assert n == 1
    assert tg.write_log(rows, log)
    again, n2 = tg.settle_rows(tg.read_log(log), {("BTC", EXP): 999.0, ("ETH", EXP): 95.0}, AFTER + 1)
    assert n2 == 1  # only the ETH record; the BTC one is not settled twice
    recs = [r for _, r in again if r is not None]
    btc, eth = recs
    assert btc["status"] == "settled" and btc["delivery_price"] == 105.0 and btc["r"] == pytest.approx(0.25)
    assert eth["status"] == "settled" and eth["pnl_per_unit"] == pytest.approx(2)
    assert [line for line, r in again if r is None] == ["{broken", "[1, 2]"]  # malformed lines kept as they were
    assert tg.settle_rows(again, {("BTC", EXP): 1.0, ("ETH", EXP): 1.0}, AFTER + 2)[1] == 0
    assert tg.read_log(tmp_path / "missing.jsonl") == []


@pytest.mark.parametrize("sep", [" ", " ", "\x85"])
def test_line_separators_inside_a_record_survive_a_rewrite(tmp_path, sep):
    """str.splitlines cuts at U+2028 / U+2029 / U+0085; an older record holding one raw (written with
    ensure_ascii=False) must stay one line through read → settle another → write → read."""
    log = tmp_path / "ideas.jsonl"
    odd = {**PUT_SPREAD, "coin": "ETH", "context": {"checks": {"P9": {"detail": f"CPI{sep}in 5 h"}}}}
    log.write_text(json.dumps(odd, ensure_ascii=False) + "\r\n" + json.dumps(CALL_SPREAD) + "\n", encoding="utf-8")
    rows = tg.read_log(log)
    assert [r is not None for _, r in rows] == [True, True]
    rows, n = tg.settle_rows(rows, {("BTC", EXP): 105.0}, AFTER)
    assert n == 1 and tg.write_log(rows, log)
    lines = log.read_text(encoding="utf-8").split("\n")
    assert len(lines) == 3 and lines[2] == ""
    assert json.loads(lines[0]) == odd and json.loads(lines[1])["status"] == "settled"
    assert sep not in lines[1]  # rewritten records are ASCII-escaped
    assert [r for _, r in tg.read_log(log)][0] == odd
    assert list(tmp_path.iterdir()) == [log]  # no temp file left behind


def test_log_ideas_writes_ascii_lines(tmp_path, p3_sendable):
    ideas, ctx = oi.ideas_for("BTC", QUOTES, [80_000 - i * 40 for i in range(260)], NOW)
    ctx = {**ctx, "note": "a b"}
    log = tmp_path / "ideas.jsonl"
    assert tg.log_ideas([(ideas, ctx)], NOW / 1000, log) == 1
    text = log.read_text(encoding="utf-8")
    assert text.isascii() and json.loads(text)["context"]["note"] == "a b"


def test_write_log_failure_returns_false(tmp_path):
    assert tg.write_log([("{}", {})], tmp_path / "no-such-dir" / "ideas.jsonl") is False


def _run_main(monkeypatch, tmp_path, argv: list[str], sent_ok: bool = True):
    """main() with the network (gather), Telegram and the journal path replaced; returns (rc, sends, log path)."""
    past = EXP - 30 * 86_400_000  # long expired whatever the clock says
    log = tmp_path / "ideas.jsonl"
    log.write_text(json.dumps({**CALL_SPREAD, "expiry_ms": past}) + "\n", encoding="utf-8")
    per = [oi.ideas_for("BTC", QUOTES, [80_000 - i * 40 for i in range(260)], NOW)]

    async def fake_gather(risk, now_ms, wanted):
        assert list(wanted) == [("BTC", past)]
        return per, {k: 105.0 for k in wanted}

    sends = []

    def fake_send(text, topic, buttons):
        sends.append((text, log.read_text(encoding="utf-8")))  # the journal as it is at sending time
        return sent_ok

    monkeypatch.setattr(tg, "IDEAS_LOG", log)
    monkeypatch.setattr(tg, "gather", fake_gather)
    monkeypatch.setattr(tg.tg_send, "send", fake_send)
    monkeypatch.setattr(tg.fw, "journal_buttons", lambda: [])
    monkeypatch.setenv("TRADEDESK_DB_PATH", str(tmp_path / "none.db"))
    rc = tg.main([*argv, "--db", str(tmp_path / "none.db")])
    return rc, sends, log


def test_main_print_writes_nothing(monkeypatch, tmp_path, capsys):
    rc, sends, log = _run_main(monkeypatch, tmp_path, ["--print"])
    before = json.dumps({**CALL_SPREAD, "expiry_ms": EXP - 30 * 86_400_000}) + "\n"
    assert rc == 0 and sends == [] and log.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "Опціонні ідеї" in out and "сума R +0.25" in out  # settled in memory only


def test_main_settles_before_sending_then_logs(monkeypatch, tmp_path, p3_sendable):
    rc, sends, log = _run_main(monkeypatch, tmp_path, [])
    assert rc == 0 and len(sends) == 1
    assert '"status": "settled"' in sends[0][1]  # the settlement was written before the message went out
    recs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [r["status"] for r in recs] == ["settled", "open"]  # then the new idea appended


def test_main_failed_send_logs_no_new_ideas(monkeypatch, tmp_path):
    rc, _, log = _run_main(monkeypatch, tmp_path, [], sent_ok=False)
    recs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert rc == 1 and [r["status"] for r in recs] == ["settled"]


class _Ex:
    """Stands in for rt.exchanges: delivery answers by symbol, raises for unknown contracts."""

    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    async def public_get(self, ex_id: str, endpoint: str, params: dict, ttl: float = 0):
        self.calls.append((ex_id, endpoint, params["symbol"]))
        if params["symbol"] not in self.answers:
            raise RuntimeError("bybit: The contract doesn't exist.")
        return type("F", (), {"data": self.answers[params["symbol"]]})()


def test_fetch_deliveries_tries_legs_and_survives_failures():
    import asyncio
    from types import SimpleNamespace

    good = {"result": {"list": [{"deliveryPrice": "67155.4", "deliveryTime": str(EXP)}]}}
    ex = _Ex({"BTC-27NOV26-110-C-USDT": good})
    wanted = {("BTC", EXP): ["BTC-27NOV26-100-C-USDT", "BTC-27NOV26-110-C-USDT"], ("ETH", EXP): ["ETH-27NOV26-2000-P-USDT"]}
    prices = asyncio.run(tg.fetch_deliveries(SimpleNamespace(exchanges=ex), wanted))
    assert prices == {("BTC", EXP): pytest.approx(67155.4)}  # ETH failed: left out, its records stay open
    assert ex.calls[0] == ("bybit", "publicGetV5MarketDeliveryPrice", "BTC-27NOV26-100-C-USDT")


def test_journal_lines_in_the_message():
    assert tg.journal_lines([]) == []
    assert tg.journal_lines([CALL_SPREAD]) == ["Журнал ідей: відкритих 1, розрахованих на експірації ще нема."]
    recs = [_settled(1.5)] * 2 + [_settled(-1.0)] + [CALL_SPREAD]
    lines = tg.journal_lines(recs)
    assert "відкритих 1" in lines[0]
    assert lines[1] == "P3 200-денний тренд → дебетовий спред: n 3, сума R +2.00, у плюсі 67% · n&lt;30, ще без висновку"  # HTML
    few = tg.journal_lines([_settled(0.5 + 0.01 * i, k=i % 9) for i in range(30)])
    assert few[1].endswith("у плюсі 100% · експірацій 9&lt;10, ще без висновку")
    lines = tg.journal_lines([_settled(0.5 + 0.01 * i, k=i) for i in range(30)])
    assert "висновок: плюс (середній R по експіраціях +0.6" in lines[1] and "експірацій 30)" in lines[1]
    assert "без висновку" not in lines[1]
    text = tg.build_message([([], {"coin": "BTC", "skip": "x"})], NOW / 1000, 100.0, journal=lines)
    assert "Журнал ідей" in text and text.index("Журнал ідей") < text.index("Не надсилаємо")
