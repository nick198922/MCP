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
from tradedesk.tools.bybit_options import DEBIT_MARGIN_WARNING

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


# ETH: the BTC book scaled by 1/25 (underlying 3200), so the fee is the capped one on the cheap wings.
U_ETH = 3_200.0
ETH_BOOK = [
    (2_960, "P", 4.0, 4.4, -0.05, 0.55),
    (3_040, "P", 16.0, 16.8, -0.15, 0.50),
    (3_120, "P", 35.2, 36.0, -0.35, 0.47),
    (3_200, "P", 79.2, 80.0, -0.50, 0.45),
    (3_200, "C", 83.2, 84.0, 0.50, 0.45),
    (3_280, "C", 36.0, 36.8, 0.30, 0.46),
    (3_360, "C", 15.2, 16.0, 0.15, 0.48),
    (3_440, "C", 3.6, 4.0, 0.05, 0.52),
]
# A 73500 put: its distance from the sold 76000 put (2500) is nearer the 3 % wing (2400) than 74000's, so the put wing
# becomes 2500 while the call wing stays 2000.
UNEVEN_PUT = (73_500, "P", 60, 70, -0.03, 0.52)


def _rows(usdc_dup: bool = False, coin: str = "BTC", uneven: bool = False) -> list[dict]:
    book, und = (ETH_BOOK, U_ETH) if coin == "ETH" else (BOOK + [UNEVEN_PUT] * uneven, U)
    rows = [
        {
            "symbol": f"{coin}-27NOV26-{k}-{t}-USDT",
            "bid1Price": str(bid),
            "ask1Price": str(ask),
            "markPrice": str((bid + ask) / 2),
            "markIv": str(iv),
            "delta": str(d),
            "underlyingPrice": str(und),
            "indexPrice": str(und),
        }
        for k, t, bid, ask, d, iv in book
    ]
    if usdc_dup:  # the same strikes settled in USDC (no suffix) with a far better bid: must never be priced
        rows += [dict(x, symbol=x["symbol"].removesuffix("-USDT"), bid1Price="5000") for x in rows]
    return rows


def hand_rt(mode: str = "ok") -> Runtime:
    class Ex(FakeEx):
        async def publicGetV5MarketTickers(self, params):  # noqa: N802 - ccxt implicit endpoint name
            if mode == "down":
                raise ccxt.NetworkError("bybit unreachable")
            coin = params.get("baseCoin")
            if coin == "BTC":
                rows = _rows(mode == "usdc_dup", uneven=mode == "uneven")
            else:
                rows = _rows(coin="ETH") if mode == "eth" else []
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
    assert d["wing_widths"] == {"put": 2000.0, "call": 2000.0}
    assert any(w.startswith("Credit structure: sold legs need margin") for w in env["warnings"])
    assert any("delivery fee" in w for w in env["warnings"]) and not any("Unequal wings" in w for w in env["warnings"])


async def test_put_debit_spread_and_straddle_match_hand_computation():
    senv = await _quote(hand_rt(), kind="put_debit_spread")
    s = senv["data"]
    # Bybit Regular margin does not net the bought leg: the sold leg's margin is named, in debit-spread words
    assert [w for w in senv["warnings"] if "margin" in w] == [DEBIT_MARGIN_WARNING] and "wing_widths" not in s
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
    # 84148 is above the highest put (80000): the delta interpolation would hold 0.50 there (true P(below) is higher),
    # overstating P(above); prob_profit is null and the warning names the breakeven and the put strike range
    assert t["prob_profit"] is None
    assert any(
        w.startswith("prob_profit is null: breakeven(s) 84148.00 outside the put strikes with a delta (74000-80000)")
        for w in env["warnings"]
    )


def test_strangle_and_straddle_prob_is_null_only_off_the_put_grid():
    now_ms = int(NOW.timestamp() * 1000)
    quotes = oi.parse_bybit_tickers(_rows(), "BTC")
    exp = oi.pick_expiry(quotes, now_ms)
    t = oi.long_straddle(quotes, exp, now_ms, "BTC")
    assert t.prob_profit is None and t.extra == {"put_strikes": [74_000.0, 80_000.0]}
    # puts extended to 90000 (|delta| 0.95): both breakevens inside -> interpolated, P(below 75852) + P(above 84148)
    wide = quotes + [oi.Quote(exp, 90_000.0, False, 9_900, 10_100, 10_000, 0.4, -0.95, U)]
    t2 = oi.long_straddle(wide, exp, now_ms, "BTC")
    p_lo = 0.05 + 0.10 * 1_852 / 2_000
    p_hi = 0.50 + 0.45 * 4_148 / 10_000
    assert t2.extra == {} and t2.prob_profit == pytest.approx(p_lo + 1 - p_hi)
    s = oi.long_strangle(quotes, exp, now_ms, "BTC", delta=0.35)  # 78000 P / 82000 C; 82000 + debit > 80000
    assert s.prob_profit is None and s.extra["put_strikes"] == [74_000.0, 80_000.0]


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


async def test_unequal_condor_wings_are_shown_and_warned():
    env = await _quote(hand_rt("uneven"))
    d = env["data"]
    assert [(g["strike"], g["side"], g["price"]) for g in d["legs"]][:2] == [(76000.0, "sell", 400.0), (73500.0, "buy", 70.0)]
    assert d["wing_widths"] == {"put": 2500.0, "call": 2000.0}
    # gross 400 + 380 - 70 - 100 = 610; fees 24 + 4.9 + 24 + 7 = 59.9; net 550.1; max loss on the wider (put) side
    # 2500 - 550.1 = 1949.9; the call side loses at most 2000 - 550.1 = 1449.9
    assert d["net_premium"] == pytest.approx(550.1) and d["max_loss"] == pytest.approx(1949.9)
    assert (
        "Unequal wings (strike grid): put wing 2500, call wing 2000 USD; max_loss (1949.90) is the wider side's, the "
        "narrower side loses at most 1449.90." in env["warnings"]
    )


async def test_min_days_above_max_days_is_named_without_a_fetch():
    rt = hand_rt("down")  # a fetch would fail: the check comes first
    env = await _quote(rt, min_days=10, max_days=5)
    assert env["ok"] is False and env["data"] is None and env["evidence"] == []
    assert [(m["field"], m["reason"], m["detail"]) for m in env["missing"]] == [
        ("min_days", "invalid_input", "min_days=10 is greater than max_days=5: the expiry window is empty")
    ]


async def test_eth_iron_condor_matches_hand_computation():
    env = await _quote(hand_rt("eth"), currency="ETH")
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert (d["currency"], d["expiry"], d["underlying"]) == ("ETH", "27NOV26", U_ETH)
    assert [(g["symbol"], g["side"], g["price"]) for g in d["legs"]] == [
        ("ETH-27NOV26-3040-P-USDT", "sell", 16.0),
        ("ETH-27NOV26-2960-P-USDT", "buy", 4.4),
        ("ETH-27NOV26-3360-C-USDT", "sell", 15.2),
        ("ETH-27NOV26-3440-C-USDT", "buy", 4.0),
    ]
    # fee = min(0.03 % x 3200 = 0.96, 7 % x price): 0.96, 0.308, 0.96, 0.28 = 2.508
    assert [g["fee"] for g in d["legs"]] == [0.96, 0.31, 0.96, 0.28] and d["fees"] == pytest.approx(2.51)
    # gross 16 + 15.2 - 4.4 - 4 = 22.8; net 20.292 credit; width 80 -> max loss 59.708
    assert d["gross_premium"] == pytest.approx(22.8) and d["net_premium"] == pytest.approx(20.29)
    assert d["max_loss"] == pytest.approx(59.71) and d["breakevens"] == [pytest.approx(3019.71), pytest.approx(3380.29)]
    assert d["wing_widths"] == {"put": 80.0, "call": 80.0}


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
