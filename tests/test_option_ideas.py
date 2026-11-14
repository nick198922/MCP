"""analytics.option_ideas and scripts/option_ideas_tg.py: structures at Bybit bid/ask, sizing, payoff, message."""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
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


def test_ideas_for_sends_trend_spread_but_never_the_x_rules():
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
    assert {c for c, r in oi.RULES.items() if not r.sendable} == {"P1", "P4", "P5", "P14", "P6", "P7", "P8", "P11", "P12", "P13"}
    assert {c for c, r in oi.RULES.items() if r.sendable} == {"P2", "P3", "P9", "P10", "P15"}


def test_rank_ideas_drops_x_and_orders_by_evidence():
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


def test_message_has_strikes_premium_rr_size_rule_and_caveat(tmp_path):
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
