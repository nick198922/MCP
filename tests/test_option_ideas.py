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


def test_usdc_rows_with_a_better_bid_never_price_an_idea():
    """scripts/option_ideas_tg.py passes every Bybit row to parse_bybit_tickers: a USDC-settled duplicate (no suffix)
    with a far better bid must not reach the quotes, so the generator and option_structure_quote agree."""
    usdc = [dict(x, symbol=x["symbol"].removesuffix("-USDT"), bid1Price="50000") for x in _chain()]
    assert oi.parse_bybit_tickers(usdc, "BTC") == []
    mixed = oi.parse_bybit_tickers(usdc + _chain(), "BTC")
    assert mixed == QUOTES
    assert oi.iron_condor(mixed, EXP, NOW, "BTC") == oi.iron_condor(QUOTES, EXP, NOW, "BTC")
    other = [dict(x, symbol=x["symbol"].replace("-USDT", "-USDC")) for x in _chain()[:4]]  # any other suffix too
    assert oi.parse_bybit_tickers(other, "BTC") == []


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
    assert c.extra == {"put_wing": 2_000, "call_wing": 2_000}
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
    assert s.extra == {} and 0 < s.prob_profit < 1  # breakevens inside the 54k..80k put strikes: interpolated


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


def test_p10_p11_carry_the_playbook_status():
    # playbook 14.11: P10 ◯ «не вимірюється на історії» (5 deals), P11 ◯ «замало даних» (n = 178)
    p10, p11 = oi.RULES["P10"], oi.RULES["P11"]
    assert p10.status == p11.status == "◯"
    assert "не вимірюється на історії" in p10.evidence and "n = 5" in p10.evidence
    assert "+0.008 R [−0.045; +0.057]" in p11.evidence and "n = 178" in p11.evidence
    assert p10.sendable and p10.kind == "trade" and not p11.sendable  # P10: a calendar since item 436
    assert {c for c, r in oi.RULES.items() if r.status == "не перевірено"} == {"P6", "P7"}


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
    # P10 met, but QUOTES hold only 27NOV26: the 20NOV / 11DEC calendar cannot be priced, so P10 stays a condition line
    assert ideas == [] and ctx["notes"] == ["P2", "P9", "P10", "P15"]
    assert ctx["unpriced"] == {"P10": "немає котирувань Bybit на 20.11 / 11.12"}
    line = tg.note_line("P10", ctx)
    assert "Правило: P10" in line and "докази: ◯ не вимірюється на історії" in line and "не перевірено" not in line
    assert line.endswith("(контекст, не угода: конструкцію за цінами Bybit не побудовано — немає котирувань Bybit на 20.11 / 11.12)")
    assert tg.note_line("P2", ctx).endswith("(контекст, не угода)")
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
            "deribit_skew_term": {"ok": True, "data": term},
            "skew_percentile": {"ok": True, "data": {"rr": {"min": -0.07, "latest": -0.05}}},
        }
    )
    m = asyncio.run(tg.market_for(c, "ETH", {"events": []}))
    assert m["vrp"]["current"]["dvol_percentile_365d"] == 0.9 and m["term"] == term and m["skew"]["rr"]["min"] == -0.07
    assert dict(c.calls)["deribit_skew_term"] == {"currency": "ETH", "view": "atm"}  # the ATM term view, not the skew one
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


def test_strangle_line_drops_the_probability_only_off_the_put_grid():
    """prob_profit comes from the shared analytics function (review 2026-11-19, finding 1): on a narrow put grid a
    strangle breakeven off the put strikes gives null, and the generator prints the line without ``(≈ N%)``."""
    full = oi.long_strangle(QUOTES, EXP, NOW, "BTC")
    assert full.prob_profit is not None  # breakevens inside the 54k..80k puts
    line = tg.idea_block(1, full, 100.0)[2]
    assert "нижче або вище за" in line and f"(≈ {full.prob_profit:.0%})" in line
    narrow = [q for q in QUOTES if q.is_call or q.strike <= 70_000]  # puts end at 70k, below the upper breakeven
    s = oi.long_strangle(narrow, EXP, NOW, "BTC")
    assert s.breakevens[1] > 70_000 and s.prob_profit is None and s.extra == {"put_strikes": [54_000.0, 70_000.0]}
    line = tg.idea_block(1, s, 100.0)[2]
    assert line.endswith(tg._h(f"нижче або вище за {' і '.join(tg._px(b, 'BTC') for b in s.breakevens)}"))
    assert "≈" not in line and "%" not in line


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
    """``pnl`` is the payoff before the delivery fee; the fee is recomputed here by the Bybit rule per ITM leg."""
    fee = 0.0
    for g in rec["legs"]:
        intrinsic = max(price - g["strike"], 0) if g["is_call"] else max(g["strike"] - price, 0)
        fee += min(0.00015 * price, 0.125 * intrinsic) if intrinsic > 0 else 0.0
    s = oi.settle_idea(rec, price, AFTER)
    assert s["status"] == "settled" and s["delivery_price"] == price and s["settled_ms"] == AFTER
    assert s["delivery_fee_per_unit"] == pytest.approx(fee, abs=1e-12)
    assert s["pnl_per_unit"] == pytest.approx(pnl - fee)
    assert s["pnl_usd"] == pytest.approx((pnl - fee) * 0.5)
    assert s["r"] == pytest.approx((pnl - fee) / rec["max_loss"])
    assert -1 - fee / rec["max_loss"] - 1e-9 <= s["r"]  # never more than the risked max loss plus the delivery fee


# A BTC-sized call debit spread: buy 70 000 C, sell 72 000 C, debit $800 after entry fees, 0.01 BTC.
BTC_CALL_SPREAD = _rec("call_debit_spread", [_leg("buy", True, 70_000), _leg("sell", True, 72_000)], net=-800, max_loss=800, qty=0.01)


@pytest.mark.parametrize(
    ("price", "fee"),
    [
        (69_000.0, 0.0),  # both legs out of the money
        (70_000.0, 0.0),  # at the long strike: not in the money, no fee
        (70_000.01, 0.125 * 0.01),  # a cent in the money: the 12.5 % cap of intrinsic binds
        (70_040.0, 5.0),  # shallow ITM: 12.5 % x 40 = 5 < 0.015 % x 70 040 = 10.506
        (71_000.0, 10.65),  # deeper: 0.015 % x 71 000 = 10.65 < 12.5 % x 1 000
        (72_000.0, 10.8),  # at the short strike: only the bought leg pays
        (73_000.0, 2 * 10.95),  # both legs in the money: each pays 0.015 % x 73 000
    ],
)
def test_settle_idea_deducts_bybit_delivery_fee_per_itm_leg(price, fee):
    s = oi.settle_idea(BTC_CALL_SPREAD, price, AFTER)
    gross = oi.payoff_at(BTC_CALL_SPREAD, price)  # payoff_at itself stays gross of the delivery fee
    assert s["delivery_fee_per_unit"] == pytest.approx(fee, abs=1e-9)
    assert s["pnl_per_unit"] == pytest.approx(gross - fee)
    assert s["r"] == pytest.approx((gross - fee) / 800)
    assert s["pnl_usd"] == pytest.approx((gross - fee) * 0.01)


def test_settle_idea_delivery_fee_example_and_several_legs():
    # 71 000: payoff +200, fee 10.65 → +189.35 per BTC, R 0.2367 (was 0.25 without the fee)
    s = oi.settle_idea(BTC_CALL_SPREAD, 71_000, AFTER)
    assert s["pnl_per_unit"] == pytest.approx(189.35) and s["r"] == pytest.approx(0.2366875)
    condor = _rec(
        "iron_condor",
        [_leg("sell", False, 60_000), _leg("buy", False, 58_000), _leg("sell", True, 72_000), _leg("buy", True, 74_000)],
        net=500,
        max_loss=1_500,
        qty=0.01,
    )
    inside = oi.settle_idea(condor, 66_000, AFTER)  # no leg in the money: no fee, the credit is kept
    assert inside["delivery_fee_per_unit"] == 0 and inside["pnl_per_unit"] == pytest.approx(500)
    up = oi.settle_idea(condor, 80_000, AFTER)  # both calls ITM (sold one too): 2 x 0.015 % x 80 000
    assert up["delivery_fee_per_unit"] == pytest.approx(24) and up["pnl_per_unit"] == pytest.approx(-1_524)
    assert up["r"] == pytest.approx(-1.016)  # a little below −1: the logged max loss has no delivery fee
    down = oi.settle_idea(condor, 50_000, AFTER)  # both puts ITM: 2 x 7.5
    assert down["delivery_fee_per_unit"] == pytest.approx(15) and down["pnl_per_unit"] == pytest.approx(-1_515)
    one = oi.settle_idea(condor, 59_000, AFTER)  # only the sold put ITM, by 1 000: 0.015 % x 59 000 = 8.85
    assert one["delivery_fee_per_unit"] == pytest.approx(8.85) and one["pnl_per_unit"] == pytest.approx(500 - 1_000 - 8.85)


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


# --- legs with their own expiry (item 421): a calendar settled at the front expiry -----------------------------------

BACK = EXP + 28 * 86_400_000  # the long leg expires 28 days after the front
CAL_IV = 0.45
CAL_K = 67_000.0
CAL_DEBIT = 1_720.0  # sold front call 1 500, bought back call 3 200, entry fees 20


def _calendar(iv: float | None = CAL_IV, back: int | None = BACK) -> dict:
    legs = [
        oi.Leg("sell", True, CAL_K, 1_500.0, -0.5),
        oi.Leg("buy", True, CAL_K, 3_200.0, 0.5, expiry_ms=back, iv=iv),
    ]
    idea = oi.Idea("BTC", "call_calendar", EXP, 13.0, SPOT, legs, -CAL_DEBIT, None, CAL_DEBIT, [], None, qty=0.01, rule="P10")
    return idea.to_dict()


def _atm_b76(f: float, iv: float, t: float) -> float:
    """Black-76 ATM call (K = F, r = 0) in closed form: F (2 N(σ√T / 2) − 1)."""
    return f * (2 * _ncdf(iv * math.sqrt(t) / 2) - 1)


def test_leg_own_expiry_defaults_to_the_idea():
    leg = oi.debit_spread(QUOTES, EXP, NOW, "BTC", bearish=True).to_dict()["legs"][0]
    assert leg["expiry_ms"] is None and leg["iv"] is None


def test_calendar_at_the_strike_is_back_time_value_minus_debit():
    t = 28 / 365
    tv = _atm_b76(CAL_K, CAL_IV, t)
    assert tv == pytest.approx(3_329, abs=1)  # ≈ 0.4 F σ √T = 3 341 (the small-σ√T approximation)
    d = _calendar()
    assert oi.payoff_at(d, CAL_K) == pytest.approx(tv - CAL_DEBIT, rel=1e-9)
    s = oi.settle_idea(d, CAL_K, AFTER)  # at the strike no leg is in the money: no delivery fee
    assert s["delivery_fee_per_unit"] == 0 and s["pnl_per_unit"] == pytest.approx(tv - CAL_DEBIT, rel=1e-9)
    assert s["r"] == pytest.approx((tv - CAL_DEBIT) / CAL_DEBIT, rel=1e-9)
    assert s["pnl_usd"] == pytest.approx((tv - CAL_DEBIT) * 0.01, rel=1e-9)


@pytest.mark.parametrize("price", [30_000.0, 130_000.0])
def test_calendar_far_from_the_strike_loses_about_the_debit(price):
    d = _calendar()
    assert oi.payoff_at(d, price) == pytest.approx(-CAL_DEBIT, abs=1.0)  # back leg ≈ intrinsic, front pays intrinsic
    assert oi.payoff_at(d, price) > -CAL_DEBIT - 1e-9  # the back leg is never worth less than its intrinsic value
    s = oi.settle_idea(d, price, AFTER)
    # only the front (delivered) leg pays Bybit's delivery fee; the back leg is not delivered at this expiry
    fee = 0.00015 * price if price > CAL_K else 0.0
    assert s["delivery_fee_per_unit"] == pytest.approx(fee)
    assert s["pnl_per_unit"] == pytest.approx(oi.payoff_at(d, price) - fee)


def test_leg_with_the_idea_expiry_keeps_intrinsic_and_old_records_settle_the_same():
    same = {**CALL_SPREAD, "legs": [{**g, "expiry_ms": EXP, "iv": 0.5} for g in CALL_SPREAD["legs"]]}
    none = {**CALL_SPREAD, "legs": [{**g, "expiry_ms": None, "iv": None} for g in CALL_SPREAD["legs"]]}
    for price in (95, 100, 104, 105, 110, 130):
        old = oi.settle_idea(CALL_SPREAD, price, AFTER)  # a journal line written before legs had an expiry
        assert oi.settle_idea(same, price, AFTER) == old and oi.settle_idea(none, price, AFTER) == old
    real = oi.iron_condor(QUOTES, EXP, NOW, "BTC").to_dict()
    stripped = {**real, "legs": [{k: v for k, v in g.items() if k not in ("expiry_ms", "iv")} for g in real["legs"]]}
    for price in (50_000, SPOT, 90_000):
        assert oi.settle_idea(stripped, price, AFTER) == oi.settle_idea(real, price, AFTER)


def test_calendar_leg_that_cannot_be_valued_is_not_settled():
    for d in (_calendar(iv=None), _calendar(iv=0.0), _calendar(back=EXP - 86_400_000)):
        assert oi.settle_idea(d, CAL_K, AFTER) is None
        assert math.isnan(oi.payoff_at(d, CAL_K))
    no_exp = {k: v for k, v in _calendar().items() if k != "expiry_ms"}
    assert math.isnan(oi.payoff_at(no_exp, CAL_K))


@pytest.mark.parametrize("drop", [True, False])
def test_calendar_without_the_idea_expiry_is_nan_and_never_settled(drop):
    """A calendar whose idea lost its expiry (key gone or null): the back leg cannot be placed in time."""
    d = _calendar()
    d = {k: v for k, v in d.items() if k != "expiry_ms"} if drop else {**d, "expiry_ms": None}
    for price in (30_000.0, CAL_K, 130_000.0):
        assert math.isnan(oi.payoff_at(d, price))
        assert oi.settle_idea(d, price, AFTER) is None
    assert tg.unsettled_count([{**d, "status": "open"}], AFTER) == 0  # no expiry: not known to be past it


def _b76_call(f: float, k: float, t: float, iv: float) -> float:
    """Black-76 call (r = 0), written out here as an independent reference."""
    d1 = (math.log(f / k) + iv * iv * t / 2) / (iv * math.sqrt(t))
    return f * _ncdf(d1) - k * _ncdf(d1 - iv * math.sqrt(t))


@pytest.mark.parametrize("price", [30_000.0, 60_000.0, CAL_K, 75_000.0, 130_000.0])
def test_put_calendar_with_a_later_put_leg_matches_the_call_calendar_by_parity(price):
    """Same strike, same debit: P = C − (F − K) and (K − F)+ = (F − K)+ − (F − K) make the payoffs equal."""
    call = _calendar()
    put = {**call, "kind": "put_calendar", "legs": [{**g, "is_call": False} for g in call["legs"]]}
    assert put["legs"][1]["expiry_ms"] == BACK and put["legs"][1]["iv"] == CAL_IV
    assert oi.payoff_at(put, price) == pytest.approx(oi.payoff_at(call, price), abs=1e-6)
    s = oi.settle_idea(put, price, AFTER)
    fee = 0.00015 * price if price < CAL_K else 0.0  # only the front (sold) put is delivered, when in the money
    assert s["delivery_fee_per_unit"] == pytest.approx(fee)
    assert s["pnl_per_unit"] == pytest.approx(oi.payoff_at(put, price) - fee)


@pytest.mark.parametrize("price", [30_000.0, CAL_K, 75_000.0, 130_000.0])
def test_reverse_calendar_sells_the_later_leg_and_mirrors_the_calendar(price):
    cal = _calendar()
    flip = {"buy": "sell", "sell": "buy"}
    rev = {**cal, "net": CAL_DEBIT, "legs": [{**g, "action": flip[g["action"]]} for g in cal["legs"]]}
    assert oi.payoff_at(rev, price) == pytest.approx(-oi.payoff_at(cal, price), abs=1e-6)
    s = oi.settle_idea(rev, price, AFTER)
    fee = 0.00015 * price if price > CAL_K else 0.0  # the bought front call is delivered; the sold back one is not
    assert s["delivery_fee_per_unit"] == pytest.approx(fee)
    assert s["pnl_per_unit"] == pytest.approx(-oi.payoff_at(cal, price) - fee)


@pytest.mark.parametrize("price", [60_000.0, 66_000.0, 66_040.0, 68_000.0, 75_000.0])
def test_reverse_diagonal_against_a_reference_black76(price):
    """Bought front call 66 000, sold back call 70 000 (28 days later, IV 0.45), net debit 300 after fees."""
    back = {**_leg("sell", True, 70_000), "expiry_ms": BACK, "iv": 0.45}
    rec = _rec("call_diagonal", [_leg("buy", True, 66_000), back], net=-300, max_loss=5_000, qty=0.01)
    expected = -300 + max(price - 66_000, 0.0) - _b76_call(price, 70_000, 28 / 365, 0.45)
    assert oi.payoff_at(rec, price) == pytest.approx(expected, rel=1e-9, abs=1e-6)
    fee = min(0.00015 * price, 0.125 * (price - 66_000)) if price > 66_000 else 0.0  # 66 040: the 12.5 % cap binds
    s = oi.settle_idea(rec, price, AFTER)
    assert s["delivery_fee_per_unit"] == pytest.approx(fee)
    assert s["pnl_per_unit"] == pytest.approx(expected - fee, rel=1e-9, abs=1e-6)
    assert s["r"] == pytest.approx((expected - fee) / 5_000, rel=1e-9, abs=1e-9)
    assert s["pnl_usd"] == pytest.approx((expected - fee) * 0.01, rel=1e-9, abs=1e-8)


def _b76_put(f: float, k: float, t: float, iv: float) -> float:
    """Black-76 put (r = 0), written out here as an independent reference: K N(−d2) − F N(−d1)."""
    d1 = (math.log(f / k) + iv * iv * t / 2) / (iv * math.sqrt(t))
    return k * _ncdf(-(d1 - iv * math.sqrt(t))) - f * _ncdf(-d1)


@pytest.mark.parametrize("price", [40_000.0, 60_000.0, 64_000.0, 67_960.0, 68_000.0, 75_000.0])
def test_reverse_put_diagonal_against_a_reference_black76(price):
    """Bought front put 68 000, sold back put 64 000 (28 days later, IV 0.45), net debit 300 after fees (item 427).
    The sold later leg is valued at forward = the delivery price, r = 0, T = 28 / 365."""
    t = 28 / 365
    back = {**_leg("sell", False, 64_000), "expiry_ms": BACK, "iv": 0.45}
    rec = _rec("put_diagonal", [_leg("buy", False, 68_000), back], net=-300, max_loss=5_000, qty=0.01)
    put = _b76_put(price, 64_000, t, 0.45)
    if price == 64_000.0:  # at the back strike: the closed-form ATM value F (2 N(σ√T / 2) − 1) ≈ 3 180
        assert put == pytest.approx(_atm_b76(64_000, 0.45, t), rel=1e-12) and put == pytest.approx(3_180, abs=1)
    assert put >= max(64_000 - price, 0.0)  # the reference itself respects the floor
    front = max(68_000 - price, 0.0)
    expected = -300 + front - put
    got = oi.payoff_at(rec, price)
    assert got == pytest.approx(expected, rel=1e-9, abs=1e-6)
    # sign: the sold later leg costs its value, so P/L sits below the front leg plus net, by at least its intrinsic
    sold_value = -300 + front - got
    assert sold_value > 0 and sold_value >= max(64_000 - price, 0.0) - 1e-9
    bought = {**rec, "legs": [rec["legs"][0], {**back, "action": "buy"}]}
    assert oi.payoff_at(bought, price) - got == pytest.approx(2 * put, rel=1e-9, abs=1e-6)
    fee = min(0.00015 * price, 0.125 * (68_000 - price)) if price < 68_000 else 0.0  # 67 960: the 12.5 % cap binds
    s = oi.settle_idea(rec, price, AFTER)  # only the front (bought) put is delivered; the sold back put is not
    assert s["delivery_fee_per_unit"] == pytest.approx(fee)
    assert s["pnl_per_unit"] == pytest.approx(expected - fee, rel=1e-9, abs=1e-6)
    assert s["r"] == pytest.approx((expected - fee) / 5_000, rel=1e-9, abs=1e-9)
    assert s["pnl_usd"] == pytest.approx((expected - fee) * 0.01, rel=1e-9, abs=1e-8)
    assert s["later_legs"] == "model" and s["close_fee_per_unit"] == 0


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
    # 105: the bought call is 5 ITM, delivery fee min(0.015 % x 105, 12.5 % x 5) = 0.01575 → (1 − 0.01575) / 4
    assert btc["status"] == "settled" and btc["delivery_price"] == 105.0 and btc["r"] == pytest.approx(0.2460625)
    assert btc["delivery_fee_per_unit"] == pytest.approx(0.01575)
    assert eth["status"] == "settled" and eth["pnl_per_unit"] == pytest.approx(2 - 0.01425)  # bought put 5 ITM at 95
    assert [line for line, r in again if r is None] == ["{broken", "[1, 2]"]  # malformed lines kept as they were
    assert tg.settle_rows(again, {("BTC", EXP): 1.0, ("ETH", EXP): 1.0}, AFTER + 2)[1] == 0
    assert tg.read_log(tmp_path / "missing.jsonl") == []


def test_settled_record_from_before_the_delivery_fee_is_not_rewritten():
    """A record settled by the old code (no delivery_fee_per_unit, R gross of the fee) keeps its line and its R."""
    old = {**CALL_SPREAD, "status": "settled", "settled_ms": AFTER, "delivery_price": 105.0, "pnl_per_unit": 1.0, "pnl_usd": 0.5, "r": 0.25}
    rows = _rows(old, {**PUT_SPREAD, "coin": "ETH"})
    out, n = tg.settle_rows(rows, {("BTC", EXP): 105.0, ("ETH", EXP): 95.0}, AFTER + 1)
    assert n == 1 and out[0] == rows[0]  # the old line is byte-for-byte the same
    assert "delivery_fee_per_unit" not in out[0][1] and out[1][1]["delivery_fee_per_unit"] == pytest.approx(0.01425)
    assert oi.rule_summary([r for _, r in out])["P3"]["sum_r"] == pytest.approx(0.25 + (2 - 0.01425) / 3)


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
        return per, {k: 105.0 for k in wanted}, {}

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


def test_journal_counts_open_records_settle_idea_refuses():
    no_iv = {**_calendar(iv=None), "status": "open"}
    early = {**_calendar(back=EXP - 86_400_000), "status": "open"}  # the "later" leg expires a day before the idea
    broken = {**CONDOR, "legs": 5}
    recs = [
        CALL_SPREAD,  # settleable, only waiting for the delivery price: not counted
        {**_calendar(), "status": "open"},  # a valid calendar: not counted
        no_iv,
        early,
        broken,
        {**no_iv, "expiry_ms": EXP + WEEK},  # not expired yet
        {**no_iv, "status": "settled"},
        _settled(0.5),
    ]
    assert tg.unsettled_count(recs, AFTER) == 3
    assert tg.unsettled_count(recs, EXP - 1) == 0
    lines = tg.journal_lines(recs, AFTER)
    assert "відкритих 6, з них не розраховано після експірації 3)" in lines[0]
    assert "не розраховано" not in tg.journal_lines(recs)[0]  # without the clock: the line as before
    assert "не розраховано" not in tg.journal_lines(recs, EXP - 1)[0]
    assert tg.journal_lines([no_iv], AFTER) == [
        "Журнал ідей: відкритих 1, з них не розраховано після експірації 1, розрахованих на експірації ще нема."
    ]
    # the same record through the journal run: a delivery price is there, settle_idea refuses, the line stays as it was
    rows = _rows(no_iv)
    out, n = tg.settle_rows(rows, {("BTC", EXP): CAL_K}, AFTER)
    assert n == 0 and out == rows
    assert tg.unsettled_count([r for _, r in out], AFTER) == 1


# --- P10 calendar in the mailing (item 436): sell the front straddle, buy the month one at Bybit prices ----------

DAY = 86_400_000
CAL_NOW = int(datetime(2026, 11, 21, 10, tzinfo=UTC).timestamp() * 1000)  # a Saturday: front 27NOV26 5.9 d out
MONTH = int(datetime(2026, 12, 25, 8, tzinfo=UTC).timestamp() * 1000)  # 25DEC26, 33.9 d out, 28 d after the front
FRONT_IV, MONTH_IV = 0.50, 0.42
T_GAP = 28 / 365  # years from the front expiry to the month one


def _b76_put(f: float, k: float, t: float, iv: float) -> float:
    return _b76_call(f, k, t, iv) - f + k  # parity, r = 0


def _expiry_rows(token: str, exp_ms: int, now_ms: int, iv: float, strikes=range(60_000, 75_000, 1_000), spread=0.05) -> list[dict]:
    """Bybit-shaped ticker rows of one expiry priced with Black-76 (r = 0, flat ``iv``) on the forward SPOT."""
    t = (exp_ms - now_ms) / DAY / 365
    rows = []
    for k in strikes:
        d1 = (math.log(SPOT / k) + iv * iv * t / 2) / (iv * math.sqrt(t))
        for typ, px, delta in (("C", _b76_call(SPOT, k, t, iv), _ncdf(d1)), ("P", _b76_put(SPOT, k, t, iv), _ncdf(d1) - 1)):
            rows.append(
                {
                    "symbol": f"BTC-{token}-{k}-{typ}-USDT",
                    "bid1Price": str(round(px * (1 - spread), 2)) if px > 5 else "0",
                    "ask1Price": str(round(px * (1 + spread), 2)),
                    "markPrice": str(px),
                    "markIv": str(iv),
                    "delta": str(delta),
                    "underlyingPrice": str(SPOT),
                }
            )
    return rows


CAL_QUOTES = oi.parse_bybit_tickers(
    _expiry_rows("27NOV26", EXP, CAL_NOW, FRONT_IV) + _expiry_rows("25DEC26", MONTH, CAL_NOW, MONTH_IV), "BTC"
)
CAL_MARKET = {
    "calendar": {"high_impact_within_48h": False, "events": [{"kind": "cpi", "hours_ahead": 72.0}]},  # before 27NOV
    "term": {
        "expiries": [
            {"expiry": "27NOV26", "days": 5.9, "atm_iv": FRONT_IV},
            {"expiry": "25DEC26", "days": 33.9, "atm_iv": MONTH_IV},  # nearest 30 d: +8 points of inversion
        ]
    },
}


def _cal_ref(price: float, debit: float, k: float = SPOT) -> float:
    """Independent reference of the calendar at the front expiry: the month straddle by Black-76 at its entry IV
    minus the sold straddle's intrinsic value, minus the debit."""
    return -debit + _b76_call(price, k, T_GAP, MONTH_IV) + _b76_put(price, k, T_GAP, MONTH_IV) - abs(price - k)


def test_p10_expiries_are_the_ones_the_condition_compares():
    cal = {"events": [{"kind": "cpi", "hours_ahead": 50.0}]}
    f, m = oi.p10_expiries(_term(0.41), cal)
    assert (oi.expiry_label(f), oi.expiry_label(m)) == ("20.11", "11.12") and datetime.fromtimestamp(f / 1000, UTC).hour == 8
    assert oi.p10_expiries(_term(0.41), {"events": []}) is None  # no event: no front
    assert oi.p10_expiries(None, cal) is None and oi.p10_expiries(_term(0.41), None) is None
    assert oi.p10_expiries(CAL_MARKET["term"], CAL_MARKET["calendar"]) == (EXP, MONTH)


def test_p10_calendar_at_bybit_bid_ask_with_fees_against_black76():
    idea, why = oi.calendar_straddle(CAL_QUOTES, EXP, MONTH, CAL_NOW, "BTC")
    assert why == "" and idea.kind == "straddle_calendar" and idea.expiry_ms == EXP and idea.days == 5.9
    book = {(q.expiry_ms, q.is_call): q for q in CAL_QUOTES if q.strike == SPOT}  # the ATM strike = the underlying
    fc, fp, mc, mp = book[(EXP, True)], book[(EXP, False)], book[(MONTH, True)], book[(MONTH, False)]
    assert [(g.action, g.is_call, g.strike, g.price, g.expiry_ms, g.iv) for g in idea.legs] == [
        ("sell", True, SPOT, fc.bid, None, None),
        ("sell", False, SPOT, fp.bid, None, None),
        ("buy", True, SPOT, mc.ask, MONTH, MONTH_IV),
        ("buy", False, SPOT, mp.ask, MONTH, MONTH_IV),
    ]
    fees = sum(min(0.0003 * SPOT, 0.07 * p) for p in (fc.bid, fp.bid, mc.ask, mp.ask))
    debit = mc.ask + mp.ask - fc.bid - fp.bid + fees
    assert idea.net == pytest.approx(-debit) and idea.max_loss == pytest.approx(debit)
    # the message's model numbers net the cost of closing the month legs as the journal does: (mark − bid) of each
    # (the model values them at ≈ the mark, the journal sells at the bid) plus two closing taker fees
    exit_cost = sum(q.mark - q.bid + min(0.0003 * SPOT, 0.07 * q.bid) for q in (mc, mp))
    assert exit_cost > 0
    # at the strike: the month ATM straddle 2 F (2 N(σ√T / 2) − 1) minus the debit (closed form) minus that cost
    assert idea.max_profit == pytest.approx(2 * _atm_b76(SPOT, MONTH_IV, T_GAP) - debit - exit_cost, rel=1e-9)
    assert idea.max_profit == pytest.approx(_cal_ref(SPOT, debit) - exit_cost, rel=1e-9)
    lo, hi = idea.breakevens
    assert lo < SPOT < hi
    assert _cal_ref(lo, debit + exit_cost) == pytest.approx(0, abs=1e-4) and _cal_ref(hi, debit + exit_cost) == pytest.approx(0, abs=1e-4)
    d = idea.to_dict()
    for price in (55_000.0, 63_000.0, SPOT, 70_500.0, 90_000.0):  # payoff_at (the journal's model) has no exit cost
        assert oi.payoff_at(d, price) == pytest.approx(_cal_ref(price, debit), rel=1e-9, abs=1e-6)
        assert oi.payoff_at(d, price) > -debit - 1e-9  # the loss is at most the debit
    # risk-neutral P(lo < S < hi) at the front expiry ≈ N(−d1(hi)) − N(−d1(lo)) at the front IV (|put delta| ≈ N(−d1))
    t = (EXP - CAL_NOW) / DAY / 365

    def below(x: float) -> float:
        return _ncdf(-(math.log(SPOT / x) + FRONT_IV**2 * t / 2) / (FRONT_IV * math.sqrt(t)))

    assert idea.prob_profit == pytest.approx(below(hi) - below(lo), abs=0.01)
    assert idea.extra["month_expiry_ms"] == MONTH and idea.extra["model_leg"] is True
    assert idea.extra["front_iv"] == pytest.approx(FRONT_IV) and idea.extra["month_iv"] == pytest.approx(MONTH_IV)
    assert idea.extra["strike_off_pct"] == 0.0  # the ATM strike is the underlying here


def test_p10_met_sends_the_calendar_instead_of_the_condition_line(tmp_path):
    ideas, ctx = oi.ideas_for("BTC", CAL_QUOTES, [60_000.0] * 260, CAL_NOW, market=CAL_MARKET)
    assert ctx["checks"]["P10"]["met"] is True and "skip" in ctx  # no 7-24 d expiry here: P10 does not need one
    assert [(i.kind, i.rule) for i in ideas] == [("straddle_calendar", "P10")]
    assert ctx["notes"] == [] and "unpriced" not in ctx
    cal = ideas[0]
    assert cal.qty == oi.size(cal, 100.0) > 0 and cal.qty * cal.max_loss <= 100.0
    text = tg.build_message([(ideas, ctx)], CAL_NOW / 1000, 100.0)
    assert "1. BTC · календар (продати тижневий стреддл, купити місячний) · 27.11 (6 дн)" in text
    assert "продати кол 67 000 (27.11) · продати пут 67 000 (27.11) · купити кол 67 000 (25.12) · купити пут 67 000 (25.12)" in text
    assert f"Дебет {tg._usd(cal.max_loss)} (ризик ≈ дебет)" in text and "куплений стреддл 25.12 закрити після експірації фронту" in text
    assert "Правило: P10 інверсія фронту перед подією → календар" in text and "контекст, не угода" not in text
    # logged, then settled at the front expiry with the month legs closed at the next run's Bybit bids (IV fell to 0.38)
    log = tmp_path / "ideas.jsonl"
    assert tg.log_ideas([(ideas, ctx)], CAL_NOW / 1000, log) == 1
    rows = tg.read_log(log)
    later = oi.parse_bybit_tickers(_expiry_rows("25DEC26", MONTH, AFTER, 0.38), "BTC")
    out, n = tg.settle_rows(rows, {("BTC", EXP): SPOT}, AFTER, {"BTC": later})
    rec = out[0][1]
    bids = {q.is_call: q.bid for q in later if q.strike == SPOT}
    cfee = sum(min(0.0003 * SPOT, 0.07 * b) for b in bids.values())
    expected = -cal.max_loss + bids[True] + bids[False] - cfee  # the front straddle expires at the strike: worth 0, no fee
    assert n == 1 and rec["status"] == "settled" and rec["later_legs"] == "bybit" and rec["close_lag_h"] == pytest.approx(1.0)
    assert rec["delivery_fee_per_unit"] == 0 and rec["close_fee_per_unit"] == pytest.approx(cfee)
    assert rec["pnl_per_unit"] == pytest.approx(expected) and rec["r"] == pytest.approx(expected / cal.max_loss)
    model, _ = tg.settle_rows(rows, {("BTC", EXP): SPOT}, AFTER)  # no quotes this run: the model value, flagged
    exit_cost = sum(q.mark - q.bid + min(0.0003 * SPOT, 0.07 * q.bid) for q in CAL_QUOTES if q.expiry_ms == MONTH and q.strike == SPOT)
    assert model[0][1]["later_legs"] == "model" and model[0][1]["pnl_per_unit"] == pytest.approx(cal.max_profit + exit_cost)


def _no_month_asks(qs: list) -> list:
    return [replace(q, ask=0.0) if q.expiry_ms == MONTH else q for q in qs]


@pytest.mark.parametrize(
    ("quotes", "reason"),
    [
        ([], "немає котирувань Bybit на 27.11 / 25.12"),  # the Bybit chain failed or is empty
        ([q for q in CAL_QUOTES if q.expiry_ms == EXP], "немає котирувань Bybit на 27.11 / 25.12"),  # no month expiry
        (_no_month_asks(CAL_QUOTES), "на 27.11 / 25.12 нема страйка з bid обох ніг фронту й ask обох місячних"),
        (
            [q for q in CAL_QUOTES if q.expiry_ms == EXP or q.strike >= 70_000],  # month listed only 4.5 % away
            "найближчий торгований страйк 70000 далі 1.5% від ціни 67000",
        ),
    ],
)
def test_p10_without_bybit_quotes_stays_a_condition_line(quotes, reason):
    ideas, ctx = oi.ideas_for("BTC", quotes, [60_000.0] * 260, CAL_NOW, market=CAL_MARKET)
    assert ideas == [] and ctx["checks"]["P10"]["met"] is True
    assert ctx["notes"] == ["P10"] and ctx["unpriced"] == {"P10": reason}
    text = tg.build_message([(ideas, ctx)], CAL_NOW / 1000, 100.0)
    assert f"(контекст, не угода: конструкцію за цінами Bybit не побудовано — {reason})" in text and "ідей нема" in text
    json.dumps(ctx)


def test_p10_calendar_on_a_coarse_month_grid_takes_the_atm_strike_of_that_grid():
    """Bybit lists ETH monthlies every 100 (≈ 6 % of the price): the nearest common strike may be beyond 1.5 % but
    within half the month's strike step, and it is still the ATM strike of that grid."""
    month = _expiry_rows("25DEC26", MONTH, CAL_NOW, MONTH_IV, strikes=range(61_000, 75_000, 4_000))  # 61, 65, 69, 73k
    quotes = oi.parse_bybit_tickers(_expiry_rows("27NOV26", EXP, CAL_NOW, FRONT_IV) + month, "BTC")
    idea, why = oi.calendar_straddle(quotes, EXP, MONTH, CAL_NOW, "BTC")
    assert why == "" and {g.strike for g in idea.legs} == {65_000.0}  # 2 000 (3.0 %) away, a tie with 69k: the lower
    exit_cost = sum(q.mark - q.bid + min(0.0003 * SPOT, 0.07 * q.bid) for q in quotes if q.expiry_ms == MONTH and q.strike == 65_000.0)
    assert idea.max_profit == pytest.approx(_cal_ref(65_000.0, idea.max_loss, k=65_000.0) - exit_cost, rel=1e-9)
    assert idea.extra["strike_off_pct"] == pytest.approx(-2_000 / SPOT)  # outside the measured 1.5 % band: logged
    holes = [q for q in quotes if q.expiry_ms == EXP or q.strike >= 69_000]  # 65k not listed: 69k is 3 % off, step unknown
    assert oi.calendar_straddle(holes, EXP, MONTH, CAL_NOW, "BTC") == (None, "найближчий торгований страйк 69000 далі 1.5% від ціни 67000")


def test_p10_calendar_rejects_misordered_expiries_and_unmet_condition():
    assert oi.calendar_straddle(CAL_QUOTES, MONTH, EXP, CAL_NOW, "BTC")[0] is None
    assert oi.calendar_straddle(CAL_QUOTES, EXP, MONTH, EXP + 1, "BTC")[0] is None  # the front already expired
    flat = {**CAL_MARKET, "term": {"expiries": [{**x, "atm_iv": MONTH_IV} for x in CAL_MARKET["term"]["expiries"]]}}
    ideas, ctx = oi.ideas_for("BTC", CAL_QUOTES, [60_000.0] * 260, CAL_NOW, market=flat)  # no inversion: nothing built
    assert ideas == [] and ctx["checks"]["P10"]["met"] is False and ctx["notes"] == [] and "unpriced" not in ctx


def test_calendar_later_leg_closed_at_the_bybit_price():
    d = _calendar()  # sold front call, bought back call (leg 1) at the entry IV 0.45
    for price in (60_000.0, CAL_K, 75_000.0):
        front = max(price - CAL_K, 0.0)
        dfee = min(0.00015 * price, 0.125 * front) if front > 0 else 0.0
        cfee = min(0.0003 * price, 0.07 * 3_000.0)
        assert oi.payoff_at(d, price, {1: 3_000.0}) == pytest.approx(-CAL_DEBIT - front + 3_000.0)
        s = oi.settle_idea(d, price, AFTER, {1: 3_000.0})
        assert s["later_legs"] == "bybit" and s["close_fee_per_unit"] == pytest.approx(cfee)
        assert s["delivery_fee_per_unit"] == pytest.approx(dfee)
        assert s["pnl_per_unit"] == pytest.approx(-CAL_DEBIT - front + 3_000.0 - dfee - cfee)
    model = oi.settle_idea(d, CAL_K, AFTER)
    assert model["later_legs"] == "model" and model["close_fee_per_unit"] == 0 and model["close_lag_h"] == pytest.approx(1.0)
    assert oi.settle_idea(d, CAL_K, EXP + 8 * 86_400_000)["close_lag_h"] == pytest.approx(192.0)  # a missed weekly run
    assert model["pnl_per_unit"] == pytest.approx(_atm_b76(CAL_K, CAL_IV, 28 / 365) - CAL_DEBIT, rel=1e-9)  # as before
    # a close for the leg expiring with the idea, or a non-positive one, is ignored
    assert oi.settle_idea(d, CAL_K, AFTER, {0: 999.0, 1: 0.0}) == model
    assert oi.settle_idea(d, CAL_K, AFTER, {1: float("nan")}) == model


def test_close_prices_bid_for_a_bought_later_leg_ask_for_a_sold_one():
    q = oi.Quote(BACK, CAL_K, True, bid=3_000.0, ask=3_100.0, mark=3_050.0, iv=0.4, delta=0.5, underlying=SPOT)
    d = _calendar()
    assert oi.close_prices(d, [q]) == {1: 3_000.0}
    flip = {"buy": "sell", "sell": "buy"}
    rev = {**d, "net": CAL_DEBIT, "legs": [{**g, "action": flip[g["action"]]} for g in d["legs"]]}
    assert oi.close_prices(rev, [q]) == {1: 3_100.0}
    assert oi.close_prices(d, [replace(q, bid=0.0)]) == {}  # no bid: valued by the model
    assert oi.close_prices(d, [replace(q, strike=CAL_K + 1_000)]) == {} and oi.close_prices(d, [replace(q, is_call=False)]) == {}
    assert oi.close_prices(d, []) == {} and oi.close_prices({"legs": 5}, [q]) == {} and oi.close_prices(None, [q]) == {}


@pytest.mark.parametrize("rec", [CALL_SPREAD, PUT_SPREAD, CONDOR, BTC_CALL_SPREAD])
def test_closes_never_change_single_expiry_settlement(rec):
    keys = {"status", "settled_ms", "delivery_price", "delivery_fee_per_unit", "pnl_per_unit", "pnl_usd", "r"}
    for price in (50, 88, 100, 105, 112, 71_000):
        old = oi.settle_idea(rec, price, AFTER)
        assert set(old) == keys  # no calendar fields on a single-expiry idea
        assert oi.settle_idea(rec, price, AFTER, {0: 5.0, 1: 7.0}) == old
        assert oi.payoff_at(rec, price, {0: 5.0, 1: 7.0}) == oi.payoff_at(rec, price)
    assert oi.close_prices(rec, CAL_QUOTES) == {}


def test_model_settled_calendars_stay_out_of_the_p10_result():
    """A calendar settled by the model (no Bybit bid for a month leg) is optimistic (no month IV drop, spread or fee):
    it must not change P10's n, R or conclusion; the journal counts it apart (critic 20.11, blocker)."""
    bybit = [{**_settled(-0.2 + 0.03 * i, "P10", k=i % 10), "later_legs": "bybit"} for i in range(30)]
    model = [{**_settled(0.9, "P10", k=i), "later_legs": "model"} for i in range(5)]
    base = oi.rule_summary(bybit)
    assert base["P10"]["n"] == 30 and base["P10"]["verdict"] == "plus"
    assert oi.rule_summary(bybit + model) == base  # without the exclusion: n 35, sum R +4.5 higher
    assert oi.rule_summary(model) == {} and all(oi.model_settled(r) for r in model) and not oi.model_settled(bybit[0])
    assert not oi.model_settled({"status": "open", "later_legs": "model"})
    head, line = tg.journal_lines(bybit + model)
    assert "розраховано моделлю (без ціни Bybit) 5, у результат не входять" in head
    assert line.startswith("P10 інверсія фронту перед подією → календар: n 30, сума R +7.05, у плюсі ") and "висновок: плюс" in line
    assert tg.journal_lines(bybit) == [head.replace("; розраховано моделлю (без ціни Bybit) 5, у результат не входять", ""), line]
    only = tg.journal_lines(model)
    assert only == ["Журнал ідей: відкритих 0; розраховано моделлю (без ціни Bybit) 5, у результат не входять, інших розрахованих ще нема."]
    # end to end: a record settle_rows closes by the model (no quotes this run) shows only in the counter
    ideas, ctx = oi.ideas_for("BTC", CAL_QUOTES, [60_000.0] * 260, CAL_NOW, market=CAL_MARKET)
    rec = {**ideas[0].to_dict(), "status": "open"}
    out, n = tg.settle_rows(_rows(rec), {("BTC", EXP): SPOT}, AFTER)
    assert n == 1 and out[0][1]["later_legs"] == "model" and oi.rule_summary([out[0][1]]) == {}
    assert "розраховано моделлю (без ціни Bybit) 1" in tg.journal_lines([out[0][1]], AFTER)[0]
