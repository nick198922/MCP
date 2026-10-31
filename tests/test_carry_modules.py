import random

import pytest

from tradedesk.analytics.carry import _Market, simulate_carry_portfolio
from tradedesk.analytics.carry_modules import (
    apr_available,
    block_bootstrap,
    capital_factor,
    cluster_bootstrap_total,
    collateral_buffer,
    diff_curve,
    holm,
    median_apr,
    paired_symbol_diff,
    pnl_by_symbol,
    simulate_carry_modules,
    tiered_haircut,
    window_weekly,
)

H8 = 8 * 3_600_000
H4 = 4 * 3_600_000
DAY = 86_400_000
T0 = 1_700_006_400_000  # a UTC midnight


def _market(rates, prices=None, start=T0):
    n_bars = len(rates) * 2 + 2
    prices = prices or [100.0] * n_bars
    funding = [{"ts_ms": start + i * H8, "rate": r} for i, r in enumerate(rates)]
    spot = [{"ts": start + i * H4, "close": prices[i]} for i in range(n_bars)]
    perp = [{"ts": start + i * H4, "close": prices[i] * 1.001, "high": prices[i] * 1.01} for i in range(n_bars)]
    return {"funding": funding, "spot": spot, "perp": perp}


def _random_markets(seed=7, n_sym=6, n_pay=150):
    rng = random.Random(seed)
    out = {}
    for k in range(n_sym):
        rates, r = [], 0.0002
        for _ in range(n_pay):
            r = 0.9 * r + 0.1 * rng.gauss(0.0003, 0.0008)
            rates.append(r)
        p, prices = 100.0, []
        for _ in range(n_pay * 2 + 2):
            p *= 1 + rng.gauss(0, 0.02)
            prices.append(p)
        out[f"S{k}"] = _market(rates, prices)
    return out


def test_base_reproduces_frozen_portfolio():
    mk = _random_markets()
    kw = dict(slots=2, entry_apr=0.2, spot_fee=0.001, perp_fee=0.0005, slippage=0.002)
    a = simulate_carry_portfolio(mk, lookback_days=7, exit_apr=0.0, **kw)
    b = simulate_carry_modules(mk, rank="apr7", exit_apr=0.0, rotation_gap=None, **kw)
    assert len(a["trades"]) == len(b["trades"]) > 0
    for x, y in zip(a["trades"], b["trades"], strict=True):
        assert (x["symbol"], x["open_ts"], x["close_ts"]) == (y["symbol"], y["open_ts"], y["close_ts"])
        assert x["pnl"] == pytest.approx(y["pnl"], abs=1e-12)
    assert [t for t, _ in a["equity"]] == [t for t, _ in b["equity"]]
    assert all(u == pytest.approx(v, abs=1e-12) for (_, u), (_, v) in zip(a["equity"], b["equity"], strict=True))


def test_apr_available_and_median_analytic():
    m = _Market(_market([0.0001] * 120), H4)
    t = T0 + 100 * H8
    assert apr_available(m, t, 30 * DAY, 7 * DAY) == pytest.approx(0.0001 * 3 * 365)
    # only 5 days of history: below the 7-day minimum
    assert apr_available(m, T0 + 15 * H8, 30 * DAY, 7 * DAY) is None
    # 10 days of history: APR over the available 10 days
    assert apr_available(m, T0 + 30 * H8, 30 * DAY, 7 * DAY) == pytest.approx(0.0001 * 3 * 365)
    m2 = _Market(_market([0.0001, 0.0003, 0.0002, 0.0009, 0.0001]), H4)
    # per-payment annualised (interval 8h) of payments 2..5 (the first has no previous interval): median of 3,2,9,1 -> 2.5
    assert median_apr(m2, T0 + 4 * H8, 30 * DAY) == pytest.approx(0.00025 * 3 * 365)


def test_persistent_rank_prefers_steady_payer():
    # P: 23 days of zero then a 7-day pump (APR7 ~ 1.1); S: steady 0.0004/8h (APR ~ 0.44).
    pump = [0.0] * 69 + [0.001] * 21 + [0.001] * 30
    steady = [0.0004] * 120
    mk = {"P": _market(pump), "S": _market(steady)}
    kw = dict(slots=1, entry_apr=0.2, spot_fee=0, perp_fee=0, slippage=0)
    base = simulate_carry_modules(mk, rank="apr7", **kw)
    h1 = simulate_carry_modules(mk, rank="min7_30", **kw)
    first_after_pump = lambda res: [tr["symbol"] for tr in res["trades"]]  # noqa: E731
    # base holds S from the start (P has APR7 = 0 then); S never exits, so P never enters in either variant
    assert first_after_pump(base) == first_after_pump(h1) == ["S"]
    # both cross the 20 % gate on the same payment: P (7-day pump, APR7 0.235, APR30 0.055) vs S (APR7 0.204,
    # APR30 0.173): base takes P, H1 takes S
    mk2 = {"P": _market([0.0] * 69 + [0.0015] * 51), "S": _market([0.00015] * 69 + [0.0004] * 51)}
    b2 = simulate_carry_modules(mk2, rank="apr7", **kw)
    h2 = simulate_carry_modules(mk2, rank="min7_30", **kw)
    assert b2["trades"][0]["symbol"] == "P"
    assert h2["trades"][0]["symbol"] == "S"


def test_negative_exit_threshold_holds_through_a_dip():
    # the dip takes APR7 down to about -3.7 %: below 0, above -10 %
    rates = [0.0005] * 30 + [-0.0002] * 16 + [0.0005] * 60
    mk = {"A": _market(rates)}
    kw = dict(slots=1, entry_apr=0.2, spot_fee=0, perp_fee=0, slippage=0)
    r0 = simulate_carry_modules(mk, exit_apr=0.0, **kw)
    r10 = simulate_carry_modules(mk, exit_apr=-0.10, **kw)
    assert sum(1 for t in r0["trades"] if t["reason"] == "exit") >= 1
    assert len(r10["trades"]) == 1 and r10["trades"][0]["reason"] == "end"


def test_rotation_replaces_worst_slot_and_pays_both_sides():
    a = [0.0003] * 120  # APR ~0.33
    b = [0.0] * 60 + [0.0015] * 60  # APR7 ~1.6 from day ~27
    mk = {"A": _market(a), "B": _market(b)}
    kw = dict(slots=1, entry_apr=0.2, spot_fee=0.001, perp_fee=0.0005, slippage=0.0)
    none = simulate_carry_modules(mk, rotation_gap=None, **kw)
    rot = simulate_carry_modules(mk, rotation_gap=0.5, **kw)
    big = simulate_carry_modules(mk, rotation_gap=5.0, **kw)
    assert [t["symbol"] for t in none["trades"]] == ["A"]
    assert [t["symbol"] for t in big["trades"]] == ["A"]
    assert [t["reason"] for t in rot["trades"]] == ["rotation", "end"]
    assert rot["stats"]["n_rotations"] == 1
    assert rot["trades"][0]["fees"] == pytest.approx(2 * 0.0015)


@pytest.mark.parametrize(
    "variant",
    [dict(rank="apr7"), dict(rank="min7_30", rotation_gap=0.25, exit_apr=-0.05), dict(rank="median30"), dict(rank="random", seed=3)],
)
def test_no_lookahead(variant):
    mk = _random_markets(seed=11)
    cut = T0 + 100 * H8
    trunc = {}
    for s, d in mk.items():
        late = {"funding": [dict(e, rate=e["rate"] * -5) if e["ts_ms"] > cut else e for e in d["funding"]]}
        late["spot"] = [dict(b, close=b["close"] * 3) if b["ts"] > cut else b for b in d["spot"]]
        late["perp"] = [dict(b, close=b["close"] * 0.5, high=b["high"] * 4) if b["ts"] > cut else b for b in d["perp"]]
        trunc[s] = late
    kw = dict(slots=2, entry_apr=0.2, slippage=0.001, **variant)
    a = simulate_carry_modules(mk, **kw)
    b = simulate_carry_modules(trunc, **kw)
    ea = [(t, v) for t, v in a["equity"] if t < cut]
    eb = [(t, v) for t, v in b["equity"] if t < cut]
    assert len(ea) > 50 and ea == eb
    ca = [(x["symbol"], x["close_ts"], x["pnl"]) for x in a["trades"] if x["close_ts"] < cut]
    cb = [(x["symbol"], x["close_ts"], x["pnl"]) for x in b["trades"] if x["close_ts"] < cut]
    assert ca == cb


def test_collateral_buffer_analytic():
    assert collateral_buffer(0.0) == 0.0
    assert collateral_buffer(0.25) == 0.0  # 0.25 + 0.0625 - 0.75 * 1.15 < 0
    assert collateral_buffer(0.6) == pytest.approx(0.5 - 0.4)  # entry margin binds
    # stress binds: h=0.5, move 1.0, premium 0.5 -> 1 + 0.1 - 0.5 * 1.5 = 0.35
    assert collateral_buffer(0.5, move=1.0, premium=0.5) == pytest.approx(0.35)
    assert capital_factor(None) == 1.5
    assert capital_factor(0.1) == 1.0


def test_tiered_haircut_piecewise():
    tiers = [{"minQty": "0", "maxQty": "100", "collateralRatio": "0.8"}, {"minQty": "100", "maxQty": "", "collateralRatio": "0.5"}]
    assert tiered_haircut(tiers, 50) == pytest.approx(0.2)
    assert tiered_haircut(tiers, 200) == pytest.approx(1 - 130 / 200)
    assert tiered_haircut([], 10) is None


def test_holm_step_down():
    r = holm({"a": 0.001, "b": 0.02, "c": 0.04}, alpha=0.025)
    assert r["a"]["pass"] and not r["b"]["pass"] and not r["c"]["pass"]
    assert r["a"]["p_holm"] == pytest.approx(0.003)
    assert r["b"]["p_holm"] == pytest.approx(0.04)
    assert r["c"]["p_holm"] == pytest.approx(0.04)


def test_window_weekly_and_bootstraps():
    wk = 7 * DAY
    mon = T0 + 4 * DAY - (T0 + 4 * DAY - 4 * DAY) % wk  # some Monday 00:00 UTC
    eq = [(mon + i * DAY, 0.01 * i) for i in range(28)]
    w = window_weekly(eq, mon + 7 * DAY)
    assert [round(x, 10) for _, x in w] == [0.07, 0.07, 0.07]
    bb = block_bootstrap([0.001] * 30, n_boot=200)
    assert bb["apr"] == pytest.approx(0.001 * 52.1775) and bb["lo"] == pytest.approx(bb["apr"]) and bb["p"] == 0
    cb = cluster_bootstrap_total({"x": 0.02, "y": 0.02}, 2.0, n_boot=200)
    assert cb["apr"] == pytest.approx(0.02) and cb["lo"] == pytest.approx(0.02)
    assert diff_curve([(1, 2.0), (2, 3.0)], [(1, 1.0), (3, 0.0)]) == [(1, 1.0)]


def test_pnl_by_symbol_window():
    trs = [{"symbol": "A", "open_ts": 1, "pnl": 0.1}, {"symbol": "A", "open_ts": 5, "pnl": 0.2}, {"symbol": "B", "open_ts": 6, "pnl": -0.1}]
    assert pnl_by_symbol(trs, 0.5, opened_from=5) == {"A": pytest.approx(0.1), "B": pytest.approx(-0.05)}
    assert paired_symbol_diff({"A": 1.0}, {"B": 2.0}) == {"A": 1.0, "B": -2.0}


def test_symbol_bootstrap_independent_of_insertion_order():
    # Review B, note 6: the CI depended on set iteration order (PYTHONHASHSEED). Keys are sorted now.
    rng = random.Random(3)
    vals = {f"S{i:02d}": rng.uniform(-0.05, 0.05) for i in range(25)}
    keys = list(vals)
    rng.shuffle(keys)
    shuffled = {k: vals[k] for k in keys}
    assert list(paired_symbol_diff(shuffled, {})) == sorted(vals)
    r1 = cluster_bootstrap_total(vals, 1.5, n_boot=500)
    r2 = cluster_bootstrap_total(shuffled, 1.5, n_boot=500)
    for k in ("apr", "lo", "hi", "p"):
        assert r1[k] == pytest.approx(r2[k], abs=1e-12)
