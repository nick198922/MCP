import random

import pytest

from tradedesk.analytics.carry_announcement import (
    announced_by,
    announcement_index,
    announcement_rule,
    carry_symbol_ids,
    first_announcement,
    paired_event_diffs,
    placebo_windows,
    window_rule,
)
from tradedesk.analytics.carry_delisting import perp_end_ms, simulate_carry_delisting

H8 = 8 * 3_600_000
H4 = 4 * 3_600_000
T0 = 1_700_006_400_000  # a UTC midnight on the 8 h grid
KW = dict(slots=2, entry_apr=0.2, spot_fee=0.001, perp_fee=0.0005, slippage=0.002)
K = 40
N_BARS = 2 * K + 60
DELAY = 300_000


def _market(rates, spot, perp_bars=None):
    perp_bars = len(spot) if perp_bars is None else perp_bars
    return {
        "funding": [{"ts_ms": T0 + i * H8, "rate": r} for i, r in enumerate(rates)],
        "spot": [{"ts": T0 + i * H4, "close": spot[i]} for i in range(len(spot))],
        "perp": [{"ts": T0 + i * H4, "close": spot[i] * 1.001, "high": spot[i] * 1.01} for i in range(perp_bars)],
    }


def _delisted():
    """Perp ends at T0 + (K-1)*8h; spot flat at 100 until then, then falls 1 % per 4h bar."""
    spot = [100.0] * (2 * K - 2) + [100.0 * (1 - 0.01 * (j + 1)) for j in range(N_BARS - (2 * K - 2))]
    return {"DEL": _market([0.001] * K, spot, perp_bars=2 * K - 2), "FILL": _market([0.0] * (N_BARS // 2), [50.0] * N_BARS)}


def _closed_before(res, cut):
    return [(t["symbol"], t["open_ts"], t["close_ts"], t["pnl"], t["reason"]) for t in res["trades"] if t["close_ts"] <= cut]


def test_symbol_ids_and_index_matching():
    assert carry_symbol_ids("1000PEPE/USDT:USDT") == {"perp": "1000PEPEUSDT", "spot_base": "PEPE"}
    assert carry_symbol_ids("1INCH/USDT:USDT") == {"perp": "1INCHUSDT", "spot_base": "1INCH"}
    assert carry_symbol_ids("1000SATS/USDT:USDT", "SATSUSDT")["spot_base"] == "SATS"
    assert carry_symbol_ids("X/USDT:USDT", "Y/USDT")["spot_base"] == "Y"
    ids = {"A/USDT:USDT": carry_symbol_ids("A/USDT:USDT"), "1000B/USDT:USDT": carry_symbol_ids("1000B/USDT:USDT")}
    rows = [
        {"ms": 30, "kind": "spot_delist", "tickers": ["A", "Z"]},
        {"ms": 10, "kind": "perp_delist", "tickers": ["AUSDT"]},
        {"ms": 10, "kind": "perp_delist", "tickers": ["AUSDT"]},  # duplicate time
        {"ms": 20, "kind": "perp_delist", "tickers": ["BUSDT"]},  # not the 1000B perp
        {"ms": 25, "kind": "spot_delist", "tickers": ["b"]},  # base token of 1000B, case-insensitive
        {"ms": 5, "kind": "listing", "tickers": ["A"]},  # ignored kind
    ]
    idx = announcement_index(rows, ids)
    assert idx == {"A/USDT:USDT": [10, 30], "1000B/USDT:USDT": [25]}
    assert not announced_by(idx, "A/USDT:USDT", 9) and announced_by(idx, "A/USDT:USDT", 10)
    assert not announced_by(idx, "C/USDT:USDT", 10**15)
    assert first_announcement(idx, "A/USDT:USDT") == 10
    assert first_announcement(idx, "A/USDT:USDT", upto=9) is None
    assert first_announcement(idx, "1000B/USDT:USDT", upto=25) == 25


def test_no_announcement_reproduces_the_walk():
    mk = _delisted()
    pe = {"DEL": perp_end_ms(mk["DEL"])}
    kw = dict(perp_end=pe, close_on_vanish=True, n_cycles=2, **KW)
    a = simulate_carry_delisting(mk, **kw)
    ex, ok = announcement_rule({})
    b = simulate_carry_delisting(mk, exit_now=ex, entry_allowed=ok, **kw)
    assert a["trades"] == b["trades"] and a["equity"] == b["equity"]


def test_exit_at_first_cycle_after_announcement_and_no_reentry():
    mk = _delisted()
    pe = {"DEL": perp_end_ms(mk["DEL"])}
    ann = T0 + 30 * H8 + 3_600_000  # perp still trades (ends T0 + 39*8h)
    ex, ok = announcement_rule({"DEL": [ann]})
    res = simulate_carry_delisting(mk, perp_end=pe, close_on_vanish=True, n_cycles=2, exit_now=ex, entry_allowed=ok, **KW)
    dl = [t for t in res["trades"] if t["symbol"] == "DEL"]
    assert len(dl) == 1
    tr = dl[0]
    assert tr["reason"] == "announcement" and not tr["perp_ended"]
    assert tr["open_ts"] == T0 + 22 * H8 + DELAY  # first cycle after the 7-day lookback
    assert tr["close_ts"] == T0 + 31 * H8 + DELAY  # first cycle at or after the announcement
    assert tr["basis"] == pytest.approx(0.0, abs=1e-12)  # spot 100, perp 100.1 at entry and at exit
    # funding: payments at T0 + 23*8h ... T0 + 31*8h (9 payments) of 0.001 x perp 100.1 / spot 100
    assert tr["funding"] == pytest.approx(9 * 0.001 * 1.001, rel=1e-12)
    assert [x["rule"] for x in res["rule_closes"]] == ["announcement"]
    # the (iii) walk instead holds into the naked-spot period and loses on the basis
    r3 = simulate_carry_delisting(mk, perp_end=pe, close_on_vanish=True, n_cycles=2, **KW)
    t3 = next(t for t in r3["trades"] if t["symbol"] == "DEL")
    assert t3["reason"] == "leg_vanished" and t3["basis"] < 0


def test_announcement_before_open_blocks_entry():
    mk = _delisted()
    ex, ok = announcement_rule({"DEL": [T0 + 20 * H8]})
    res = simulate_carry_delisting(mk, perp_end={"DEL": perp_end_ms(mk["DEL"])}, exit_now=ex, entry_allowed=ok, **KW)
    assert not [t for t in res["trades"] if t["symbol"] == "DEL"]


def test_exit_rule_is_causal():
    """Nothing decided at or before ``cut`` changes when announcements, prices and funding after ``cut`` change."""
    mk = _delisted()
    ann = T0 + 30 * H8 + 3_600_000
    cut = T0 + 33 * H8
    pe = {"DEL": perp_end_ms(mk["DEL"])}
    kw = dict(perp_end=pe, close_on_vanish=True, n_cycles=2, **KW)
    ex, ok = announcement_rule({"DEL": [ann]})
    a = simulate_carry_delisting(mk, exit_now=ex, entry_allowed=ok, **kw)

    mk2 = _delisted()
    for b in mk2["DEL"]["spot"] + mk2["DEL"]["perp"]:
        if b["ts"] + H4 > cut:
            b["close"] *= 3.0
    for e in mk2["DEL"]["funding"] + mk2["FILL"]["funding"]:
        if e["ts_ms"] > cut:
            e["rate"] = 0.004
    ex2, ok2 = announcement_rule({"DEL": [ann, cut + H8], "FILL": [cut + 30 * H8]})  # extra announcements after cut
    b2 = simulate_carry_delisting(mk2, exit_now=ex2, entry_allowed=ok2, **kw)
    assert _closed_before(a, cut) == _closed_before(b2, cut) and _closed_before(a, cut)
    assert [x for x in a["equity"] if x[0] <= cut] == [x for x in b2["equity"] if x[0] <= cut]
    # FILL gets hot funding after cut and is entered, then exited on its own later announcement
    assert a["equity"] != b2["equity"]
    assert [t["reason"] for t in b2["trades"] if t["symbol"] == "FILL"] == ["announcement"]

    # an announcement moved to after cut: the past up to cut equals the walk without any announcement
    ex3, ok3 = announcement_rule({"DEL": [cut + 1]})
    c = simulate_carry_delisting(mk, exit_now=ex3, entry_allowed=ok3, **kw)
    d = simulate_carry_delisting(mk, **kw)
    assert [x for x in c["equity"] if x[0] <= cut] == [x for x in d["equity"] if x[0] <= cut]


def test_window_rule_and_placebo_windows():
    inside, ok = window_rule({"S": [(100, 200), (300, 400)]})
    assert [inside("S", t) for t in (99, 100, 200, 250, 300, 401)] == [False, True, True, False, True, False]
    assert ok("S", 250) and not ok("S", 150) and ok("Q", 150)
    trades = [{"symbol": f"S{i}", "open_ts": 0, "close_ts": 10 * H8} for i in range(5)]
    trades.append({"symbol": "SHORT", "open_ts": 0, "close_ts": H8})  # too short: never picked
    w = placebo_windows(trades, [2 * H8], 3, random.Random(1))
    assert sum(len(v) for v in w.values()) == 3 and "SHORT" not in w
    assert all(v == [(8 * H8, 10 * H8)] for v in w.values())
    assert placebo_windows(trades, [2 * H8], 3, random.Random(1)) == w  # deterministic for a seed
    assert placebo_windows(trades, [20 * H8], 3, random.Random(1)) == {}  # lead longer than every trade


def test_paired_event_diffs_missing_counts_zero():
    a = [{"symbol": "X", "open_ts": 1, "pnl": 0.03}]
    b = [{"symbol": "X", "open_ts": 1, "pnl": -0.02}, {"symbol": "Y", "open_ts": 2, "pnl": -0.05}]
    out = paired_event_diffs(a, b, [("X", 1), ("Y", 2)])
    assert [round(x["diff"], 12) for x in out] == [0.05, 0.05]
