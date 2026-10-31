import random

import pytest

from tradedesk.analytics.carry import simulate_carry_portfolio
from tradedesk.analytics.carry_delisting import (
    interval_change_before,
    missing_cycles,
    perp_end_ms,
    simulate_carry_delisting,
    vanish_events,
)

H8 = 8 * 3_600_000
H4 = 4 * 3_600_000
DAY = 86_400_000
T0 = 1_700_006_400_000  # a UTC midnight
KW = dict(slots=2, entry_apr=0.2, spot_fee=0.001, perp_fee=0.0005, slippage=0.002)
K = 40  # funding payments of the delisted symbol
N_BARS = 2 * K + 60


def _market(rates, spot, perp_bars=None, start=T0):
    perp_bars = len(spot) if perp_bars is None else perp_bars
    return {
        "funding": [{"ts_ms": start + i * H8, "rate": r} for i, r in enumerate(rates)],
        "spot": [{"ts": start + i * H4, "close": spot[i]} for i in range(len(spot))],
        "perp": [{"ts": start + i * H4, "close": spot[i] * 1.001, "high": spot[i] * 1.01} for i in range(perp_bars)],
    }


def _delisted():
    """Perp funding and bars stop at T0 + (K-1)*8h; spot then falls 1 % per 4h bar."""
    spot = [100.0] * (2 * K - 2) + [100.0 * (1 - 0.01 * (j + 1)) for j in range(N_BARS - (2 * K - 2))]
    mk = {"DEL": _market([0.001] * K, spot, perp_bars=2 * K - 2)}
    mk["FILL"] = _market([0.0] * (N_BARS // 2), [50.0] * N_BARS)  # never entered; keeps the walk going
    return mk, spot


def _random_markets(seed=7, n_sym=6, n_pay=300):
    rng = random.Random(seed)
    out = {}
    for k in range(n_sym):
        rates, r = [], 0.0002
        for _ in range(n_pay):
            r = 0.9 * r + 0.1 * rng.gauss(0.0002, 0.004)
            rates.append(r)
        p, prices = 100.0, []
        for _ in range(n_pay * 2 + 2):
            p *= 1 + rng.gauss(0, 0.02)
            prices.append(p)
        out[f"S{k}"] = _market(rates, prices)
    return out


def test_without_events_reproduces_frozen_portfolio():
    mk = _random_markets()
    a = simulate_carry_portfolio(mk, **KW)
    b = simulate_carry_delisting(mk, **KW)
    assert len(a["trades"]) == len(b["trades"]) > 3
    for x, y in zip(a["trades"], b["trades"], strict=True):
        assert (x["symbol"], x["open_ts"], x["close_ts"]) == (y["symbol"], y["open_ts"], y["close_ts"])
        assert x["pnl"] == pytest.approx(y["pnl"], abs=1e-12)
    assert [t for t, _ in a["equity"]] == [t for t, _ in b["equity"]]
    assert all(u == pytest.approx(v, abs=1e-12) for (_, u), (_, v) in zip(a["equity"], b["equity"], strict=True))


def test_missing_cycles_counts_back_from_t():
    f = [T0 + i * H8 for i in range(5)]  # last payment T0 + 32h
    p = [T0 + (i + 1) * H4 for i in range(8)]  # last bar close T0 + 32h
    last = T0 + 4 * H8
    assert missing_cycles(f, p, last + 300_000) == 0
    assert missing_cycles(f, p, last + H8 + 300_000) == 1
    assert missing_cycles(f, p, last + 3 * H8 + 300_000) == 3
    assert missing_cycles(f, p, last + 3 * H8 + 300_000, max_n=2) == 2
    # bars alone keep the leg alive
    assert missing_cycles(f, p + [last + H8], last + H8 + 300_000) == 0


def test_frozen_backtest_hides_the_naked_spot_loss():
    mk, _ = _delisted()
    res = simulate_carry_portfolio(mk, **KW)
    tr = next(t for t in res["trades"] if t["symbol"] == "DEL")
    assert tr["basis"] == pytest.approx(0.0, abs=1e-12)  # both legs frozen at the last joint bar


def test_accounting_marks_spot_until_frozen_exit():
    mk, spot = _delisted()
    pe = perp_end_ms(mk["DEL"])
    last_pay = T0 + (K - 1) * H8
    assert pe == last_pay
    res = simulate_carry_delisting(mk, perp_end={"DEL": pe}, **KW)
    tr = next(t for t in res["trades"] if t["symbol"] == "DEL")
    # trailing 7d APR reaches 0 at the first cycle with no payment in (t - 7d, t]: last + 21 cycles + 5 min
    assert tr["close_ts"] == last_pay + 21 * H8 + 300_000
    i = (tr["close_ts"] - 300_000 - T0) // H4 - 1  # bar closing at the decision time
    assert tr["basis"] == pytest.approx((spot[i] - 100.0) / 100.0, abs=1e-12)  # perp settled at its entry price
    assert tr["perp_ended"] and tr["reason"] == "apr<=exit"


def test_settlement_at_spot_proxy():
    mk, spot = _delisted()
    res = simulate_carry_delisting(mk, perp_end={"DEL": perp_end_ms(mk["DEL"])}, settle="spot", **KW)
    tr = next(t for t in res["trades"] if t["symbol"] == "DEL")
    i = (tr["close_ts"] - 300_000 - T0) // H4 - 1
    # perp entered at 100.1, settled at the spot 100.0: the short gains 0.1 / 100 of the spot notional
    assert tr["basis"] == pytest.approx((spot[i] - 100.0) / 100.0 + 0.001, abs=1e-12)
    with pytest.raises(ValueError):
        simulate_carry_delisting(mk, perp_end={"DEL": perp_end_ms(mk["DEL"])}, settle="mark", **KW)


def test_rule_closes_on_the_nth_missing_cycle():
    mk, spot = _delisted()
    last_pay = T0 + (K - 1) * H8
    for n in (1, 2, 3):
        res = simulate_carry_delisting(mk, perp_end={"DEL": perp_end_ms(mk["DEL"])}, close_on_vanish=True, n_cycles=n, **KW)
        tr = next(t for t in res["trades"] if t["symbol"] == "DEL")
        assert tr["reason"] == "leg_vanished"
        assert tr["close_ts"] == last_pay + n * H8 + 300_000
        i = (tr["close_ts"] - 300_000 - T0) // H4 - 1
        assert tr["basis"] == pytest.approx((spot[i] - 100.0) / 100.0, abs=1e-12)
        assert len(res["rule_closes"]) == 1


def test_rule_is_causal():
    mk, _ = _delisted()
    cut = T0 + (K + 5) * H8
    kw = dict(close_on_vanish=True, n_cycles=2, **KW)
    a = simulate_carry_delisting(mk, **kw)
    mk2, _ = _delisted()
    for b in mk2["DEL"]["spot"]:
        if b["ts"] + H4 > cut:
            b["close"] *= 3.0
    b2 = simulate_carry_delisting(mk2, **kw)
    before = [(t["symbol"], t["open_ts"], t["close_ts"], t["pnl"]) for t in a["trades"] if t["close_ts"] <= cut]
    after = [(t["symbol"], t["open_ts"], t["close_ts"], t["pnl"]) for t in b2["trades"] if t["close_ts"] <= cut]
    assert before == after and before
    assert [x for x in a["equity"] if x[0] <= cut] == [x for x in b2["equity"] if x[0] <= cut]


def _closed_before(res, cut):
    return [(t["symbol"], t["open_ts"], t["close_ts"], t["pnl"], t["reason"]) for t in res["trades"] if t["close_ts"] <= cut]


def test_rule_is_causal_in_its_own_inputs():
    """The rule reads funding and perp bars: after the trigger the perp 'comes back' (new bars, new funding);
    nothing decided at or before ``cut`` may change."""
    mk, spot = _delisted()
    last_pay = T0 + (K - 1) * H8
    trigger = last_pay + 2 * H8 + 300_000
    cut = T0 + (K + 5) * H8
    assert trigger < cut
    kw = dict(close_on_vanish=True, n_cycles=2, **KW)
    a = simulate_carry_delisting(mk, **kw)
    assert [x["t"] for x in a["rule_closes"]] == [trigger]

    mk2, _ = _delisted()
    d = mk2["DEL"]
    d["funding"] += [{"ts_ms": T0 + j * H8, "rate": 0.003} for j in range(K + 6, N_BARS // 2)]
    d["perp"] += [{"ts": T0 + i * H4, "close": spot[i] * 1.5, "high": spot[i] * 1.6} for i in range(2 * (K + 5), N_BARS)]
    assert min(e["ts_ms"] for e in d["funding"][K:]) > cut and min(b["ts"] + H4 for b in d["perp"][2 * K - 2 :]) > cut
    # other markets' future changes as well
    for e in mk2["FILL"]["funding"]:
        if e["ts_ms"] > cut:
            e["rate"] = 0.004
    b2 = simulate_carry_delisting(mk2, **kw)

    assert _closed_before(a, cut) == _closed_before(b2, cut) and _closed_before(a, cut)
    assert [x for x in a["rule_closes"] if x["t"] <= cut] == [x for x in b2["rule_closes"] if x["t"] <= cut]
    assert [x for x in a["equity"] if x[0] <= cut] == [x for x in b2["equity"] if x[0] <= cut]
    # the perp's return is seen afterwards (re-entry or later data), so the walks do diverge after cut
    assert a["equity"] != b2["equity"] or a["trades"] != b2["trades"]

    # the same with the after-the-fact accounting on: perp_end of the modified data moves, the past must not
    pa = simulate_carry_delisting(mk, perp_end={"DEL": perp_end_ms(mk["DEL"])}, **kw)
    pb = simulate_carry_delisting(mk2, perp_end={"DEL": perp_end_ms(mk["DEL"])}, **kw)
    assert _closed_before(pa, cut) == _closed_before(pb, cut)
    assert [x for x in pa["equity"] if x[0] <= cut] == [x for x in pb["equity"] if x[0] <= cut]


def test_vanish_events_and_interval_change():
    mk, _ = _delisted()
    end = T0 + (N_BARS // 2) * H8
    ev = vanish_events(mk, end, n_cycles=2)
    assert set(ev) == {"DEL"} and ev["DEL"]["spot_after"] is True
    rows = [[T0 + i * H8, 8.0, 0.0] for i in range(10)] + [[T0 + 10 * H8 + i * H4, 4.0, 0.0] for i in range(5)]
    end_ts = T0 + 10 * H8 + 4 * H4
    ch = interval_change_before(rows, end_ts)
    assert ch["changed"] and (ch["from_h"], ch["to_h"]) == (8.0, 4.0) and ch["at_ms"] == T0 + 10 * H8
    assert not interval_change_before(rows, end_ts + 40 * DAY)["changed"]
