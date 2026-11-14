"""analytics.option_ideas and scripts/option_ideas_tg.py: structures at Bybit bid/ask, sizing, payoff, message."""

from __future__ import annotations

import importlib.util
import json
import math
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


def test_ideas_for_picks_by_iv_vs_rv_and_trend():
    down = [80_000 - i * 40 for i in range(260)]  # falling, tiny realized vol → IV ≫ RV → condor
    ideas, ctx = oi.ideas_for("BTC", QUOTES, down, NOW)
    assert ctx["trend"] == "bear" and [i.kind for i in ideas] == ["iron_condor", "put_debit_spread"]
    assert all(i.why and i.qty > 0 for i in ideas)
    wild = [60_000 * (1.06 if i % 2 else 1.0) for i in range(260)]  # RV ≫ IV → strangle, no trend
    ideas, ctx = oi.ideas_for("BTC", QUOTES, wild, NOW)
    assert [i.kind for i in ideas] == ["long_strangle"] and ctx["trend"] == "neutral"
    ideas, ctx = oi.ideas_for("BTC", [], down, NOW)
    assert ideas == [] and "skip" in ctx


_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("option_ideas_tg", _SCRIPTS / "option_ideas_tg.py")
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)


def test_message_has_strikes_premium_rr_size_and_caveat(tmp_path):
    down = [80_000 - i * 40 for i in range(260)]
    per = [oi.ideas_for("BTC", QUOTES, down, NOW), ([], {"coin": "ETH", "skip": "TimeoutError"})]
    text = tg.build_message(per, NOW / 1000, 100.0, "Журнал прогнозів: Brier 0.278")
    assert "Опціонні ідеї" in text and "залізний кондор" in text and "пут-спред" in text
    assert "прибуток : ризик" in text and "Розмір під $100" in text and "27.11" in text
    assert "ризик-нейтральні" in text and "Brier 0.278" in text and "ETH · тренд" in text
    log = tmp_path / "ideas.jsonl"
    assert tg.log_ideas(per, NOW / 1000, log) == 2
    row = json.loads(log.read_text().splitlines()[0])
    assert row["status"] == "open" and row["coin"] == "BTC" and len(row["legs"]) == 4
    assert oi.payoff_at(row, SPOT) == pytest.approx(row["max_profit"])


def test_message_without_ideas():
    text = tg.build_message([([], {"coin": "BTC", "skip": "x"}), ([], {"coin": "ETH", "skip": "x"})], NOW / 1000, 100.0)
    assert "ідей нема" in text
