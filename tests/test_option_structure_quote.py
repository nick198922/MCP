"""option_structure_quote (standing card IDEA-20261102, item 410): an option structure priced on the live Bybit chain with
the generator's functions (analytics/option_ideas.py). Fakes only, no network.

Two chains: a hand-made one whose legs, fees, net premium, max loss / profit and breakevens are computed by hand below
(the analytical reference), and the captured sample tests/fixtures/bybit_option_chain_btc_2026-10-23.json, on which
the tool must give the same numbers as the generator's own call (scripts/option_ideas_tg.py builds its ideas with
``parse_bybit_tickers`` + ``pick_expiry`` + the structure function, as here)."""

from __future__ import annotations

from datetime import UTC, datetime

import ccxt
import pytest

from conftest import FakeEx, State, make_transports
from test_bybit_option_chain import FX_NOW, ROWS, chain_rt
from test_server import call
from tradedesk.analytics import option_ideas as oi
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

NOW = datetime(2026, 11, 13, 8, tzinfo=UTC)  # 14.0 days before the 27NOV26 08:00 UTC expiry
U = 80_000.0
# (strike, type, bid, ask, Bybit delta, mark IV): eight two-sided quotes, enough for pick_expiry (min_liquid=8).
BOOK = [
    (74_000, "P", 100, 110, -0.05, 0.50),
    (76_000, "P", 400, 420, -0.15, 0.45),
    (78_000, "P", 880, 900, -0.35, 0.42),
    (80_000, "P", 1_980, 2_000, -0.50, 0.40),
    (80_000, "C", 2_080, 2_100, 0.50, 0.40),
    (82_000, "C", 900, 920, 0.30, 0.41),
    (84_000, "C", 380, 400, 0.15, 0.43),
    (86_000, "C", 90, 100, 0.05, 0.47),
]


def _rows(usdc_dup: bool = False) -> list[dict]:
    rows = [
        {
            "symbol": f"BTC-27NOV26-{k}-{t}-USDT",
            "bid1Price": str(bid),
            "ask1Price": str(ask),
            "markPrice": str((bid + ask) / 2),
            "markIv": str(iv),
            "delta": str(d),
            "underlyingPrice": str(U),
            "indexPrice": str(U),
        }
        for k, t, bid, ask, d, iv in BOOK
    ]
    if usdc_dup:  # the same strikes settled in USDC (no suffix) with a far better bid: must never be priced
        rows += [dict(x, symbol=x["symbol"].removesuffix("-USDT"), bid1Price="5000") for x in rows]
    return rows


def hand_rt(mode: str = "ok") -> Runtime:
    class Ex(FakeEx):
        async def publicGetV5MarketTickers(self, params):  # noqa: N802 - ccxt implicit endpoint name
            if mode == "down":
                raise ccxt.NetworkError("bybit unreachable")
            rows = _rows(mode == "usdc_dup") if params.get("baseCoin") == "BTC" else []
            return {"retCode": 0, "result": {"category": "option", "list": rows}, "time": int(NOW.timestamp() * 1000)}

    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
        transports=make_transports(State()),
        exchange_factory=Ex,
    )
    rt.now = lambda: NOW
    return rt


async def _quote(rt: Runtime, **args) -> dict:
    srv, _ = build_server(rt)
    try:
        return await call(srv, "option_structure_quote", **args)
    finally:
        await rt.aclose()


# ---------- analytical reference (hand-made chain) ----------


async def test_iron_condor_matches_hand_computation():
    env = await _quote(hand_rt())
    assert env["ok"] is True and env["missing"] == []
    assert env["evidence"][0]["provider"] == "ccxt:bybit"
    d = env["data"]
    assert (d["currency"], d["kind"], d["expiry"], d["days"], d["underlying"]) == ("BTC", "iron_condor", "27NOV26", 14.0, U)
    assert d["params"] == {"short_delta": 0.15, "wing": 0.03}
    # sold 76000 P (|delta| 0.15) at bid 400, bought 74000 P at ask 110; sold 84000 C at bid 380, bought 86000 C at ask 100
    assert [(g["symbol"], g["side"], g["price"]) for g in d["legs"]] == [
        ("BTC-27NOV26-76000-P-USDT", "sell", 400.0),
        ("BTC-27NOV26-74000-P-USDT", "buy", 110.0),
        ("BTC-27NOV26-84000-C-USDT", "sell", 380.0),
        ("BTC-27NOV26-86000-C-USDT", "buy", 100.0),
    ]
    assert (d["legs"][0]["bid"], d["legs"][0]["ask"], d["legs"][0]["mark_iv"], d["legs"][0]["delta"]) == (400.0, 420.0, 0.45, -0.15)
    # fee = min(0.03 % x 80000 = 24, 7 % x price): 24, 7.7, 24, 7.0 = 62.7
    assert [g["fee"] for g in d["legs"]] == [24.0, 7.7, 24.0, 7.0] and d["fees"] == pytest.approx(62.7)
    # gross 400 + 380 - 110 - 100 = 570; net 570 - 62.7 = 507.3 credit; width 2000 -> max loss 1492.7
    assert (d["gross_premium"], d["net_premium"], d["net_type"]) == (570.0, pytest.approx(507.3), "credit")
    assert d["max_profit"] == pytest.approx(507.3) and d["max_loss"] == pytest.approx(1492.7)
    assert d["breakevens"] == [pytest.approx(75492.7), pytest.approx(84507.3)]
    assert d["reward_to_risk"] == pytest.approx(507.3 / 1492.7, abs=1e-4)
    # P(below 75492.7) = 0.05 + 0.10 x 1492.7 / 2000 = 0.124635 (put deltas); P(below 84507.3) = 0.50 (last put)
    assert d["prob_profit"] == pytest.approx(0.5 - 0.124635, abs=1e-4)
    assert d["atm_iv"] == pytest.approx(0.40)
    assert any("margin" in w for w in env["warnings"]) and any("delivery fee" in w for w in env["warnings"])


async def test_put_debit_spread_and_straddle_match_hand_computation():
    s = (await _quote(hand_rt(), kind="put_debit_spread"))["data"]
    # buy 78000 P (|delta| 0.35, nearest 0.40) at ask 900, sell 76000 P (0.15) at bid 400; fees 24 + 24
    assert [(g["strike"], g["side"]) for g in s["legs"]] == [(78000.0, "buy"), (76000.0, "sell")]
    assert (s["gross_premium"], s["net_premium"], s["net_type"]) == (-500.0, -548.0, "debit")
    assert (s["max_loss"], s["max_profit"], s["breakevens"]) == (548.0, 1452.0, [77452.0])
    assert s["reward_to_risk"] == pytest.approx(1452 / 548, abs=1e-4) and s["prob_max_profit"] == 0.15
    env = await _quote(hand_rt(), kind="long_straddle")
    t = env["data"]
    # buy the 80000 put at 2000 and call at 2100, fees 24 + 24: debit 4148, breakevens 80000 -+ 4148
    assert [(g["type"], g["strike"], g["price"]) for g in t["legs"]] == [("put", 80000.0, 2000.0), ("call", 80000.0, 2100.0)]
    assert (t["net_premium"], t["max_loss"], t["max_profit"], t["reward_to_risk"]) == (-4148.0, 4148.0, None, None)
    assert t["breakevens"] == [75852.0, 84148.0]
    assert not any("margin" in w for w in env["warnings"])  # nothing sold


def test_long_straddle_payoff_is_the_debit_at_the_strike_and_zero_at_breakevens():
    quotes = oi.parse_bybit_tickers(_rows(), "BTC")
    exp = oi.pick_expiry(quotes, int(NOW.timestamp() * 1000))
    d = oi.long_straddle(quotes, exp, int(NOW.timestamp() * 1000), "BTC").to_dict()
    assert oi.payoff_at(d, 80_000) == pytest.approx(-4148.0)
    assert oi.payoff_at(d, 75_852) == pytest.approx(0, abs=1e-9) and oi.payoff_at(d, 84_148) == pytest.approx(0, abs=1e-9)
    assert oi.long_straddle(quotes, exp + 1, 0, "BTC") is None  # no quotes at that expiry


async def test_usdc_rows_are_not_priced():
    plain, dup = await _quote(hand_rt()), await _quote(hand_rt("usdc_dup"))
    assert dup["data"] == plain["data"]
    assert dup["warnings"][0] == "BTC: Bybit option symbols with settle suffix none (USDC) skipped (only USDT is priced)"


async def test_ignored_parameter_is_named():
    env = await _quote(hand_rt(), kind="long_straddle", wing=0.05, short_delta=0.2)
    assert env["ok"] and env["warnings"][0] == "short_delta, wing not used by long_straddle; ignored"


async def test_no_expiry_in_window_is_missing():
    env = await _quote(hand_rt(), days=3, min_days=0, max_days=5)
    assert env["ok"] is False and env["data"] is None
    m = env["missing"][0]
    assert (m["field"], m["reason"]) == ("expiry", "data_unavailable") and "27NOV26 (14.0 d, 8 two-sided)" in m["detail"]


async def test_unbuildable_structure_is_missing():
    # long leg at |delta| 0.05 is the lowest put: no further strike to sell
    env = await _quote(hand_rt(), kind="put_debit_spread", long_delta=0.05)
    assert env["ok"] is False and env["missing"][0]["field"] == "structure"


async def test_provider_failure_goes_to_missing():
    env = await _quote(hand_rt("down"))
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["provider"], m["reason"]) for m in env["missing"]] == [("bybit_options:BTC", "ccxt:bybit", "network")]


async def test_empty_chain_is_missing():
    env = await _quote(hand_rt(), currency="ETH")
    assert env["ok"] is False and env["missing"][0]["reason"] == "data_unavailable"


# ---------- the same numbers as the generator, on the captured sample ----------


@pytest.mark.parametrize("kind", ["iron_condor", "call_debit_spread", "put_debit_spread", "long_strangle"])
async def test_sample_matches_the_generator(kind):
    now_ms = int(FX_NOW.timestamp() * 1000)
    quotes = oi.parse_bybit_tickers(ROWS, "BTC")
    exp = oi.pick_expiry(quotes, now_ms)  # the generator's call: 14 days in [7, 24]
    idea = {
        "iron_condor": lambda: oi.iron_condor(quotes, exp, now_ms, "BTC"),
        "call_debit_spread": lambda: oi.debit_spread(quotes, exp, now_ms, "BTC", bearish=False),
        "put_debit_spread": lambda: oi.debit_spread(quotes, exp, now_ms, "BTC", bearish=True),
        "long_strangle": lambda: oi.long_strangle(quotes, exp, now_ms, "BTC"),
    }[kind]()
    env = await _quote(chain_rt(), kind=kind)
    if idea is None:
        assert env["ok"] is False and env["missing"][0]["field"] == "structure"
        return
    d = env["data"]
    assert env["ok"] and d["expiry"] == "6NOV26"
    assert [(g["strike"], g["side"], g["price"]) for g in d["legs"]] == [(g.strike, g.action, g.price) for g in idea.legs]
    assert d["net_premium"] == pytest.approx(idea.net, abs=0.005) and d["max_loss"] == pytest.approx(idea.max_loss, abs=0.005)
    assert d["breakevens"] == [pytest.approx(x, abs=0.005) for x in idea.breakevens]
    assert d["gross_premium"] - d["fees"] == pytest.approx(d["net_premium"], abs=0.02)
    for g in d["legs"]:  # symbols exist in the sample and the quoted bid / ask are its own
        row = next(x for x in ROWS if x["symbol"] == g["symbol"])
        assert g["price"] == float(row["bid1Price"] if g["side"] == "sell" else row["ask1Price"])
