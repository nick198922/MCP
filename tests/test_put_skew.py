"""Put-skew analytics (pure) and the Deribit history / delivery / funding provider methods."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.options import black76_greeks, black76_price
from tradedesk.analytics.put_skew import (
    DAY_MS,
    HOUR_MS,
    V1_BUILD,
    block_bootstrap_ci,
    build_day,
    chain,
    choose_expiry,
    dvol_known_at_entry,
    funding_sum,
    implied_forward,
    implied_vol_inverse,
    inverse_price,
    is_monthly_expiry,
    iv_at_put_delta,
    iv_at_strike,
    plan_entry,
    rr25,
    run_book,
    tercile_spread,
    trailing_percentile,
    year_frac,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.deribit import HISTORY_URL, Deribit

FIX = Path(__file__).parent / "fixtures"
D0 = 1_655_251_200_000  # 2022-06-15 00:00 UTC
EXP = D0 + 2 * DAY_MS + 8 * HOUR_MS  # 2022-06-17 08:00 UTC


# ---------- pricing primitives ----------


def test_inverse_iv_roundtrip():
    for is_call in (True, False):
        p = inverse_price(30_000, 27_000, 30 / 365, 0.62, is_call)
        assert implied_vol_inverse(p, 30_000, 27_000, 30 / 365, is_call) == pytest.approx(0.62, abs=1e-6)
    assert implied_vol_inverse(0.95, 30_000, 27_000, 30 / 365, False) is None  # above the K/F upper bound


def test_implied_forward_roundtrip():
    for is_call in (True, False):
        p = inverse_price(30_300, 29_000, 20 / 365, 0.55, is_call)
        assert implied_forward(p, 29_000, 20 / 365, 0.55, is_call, 30_000) == pytest.approx(30_300, rel=1e-6)


def test_implied_forward_on_real_print():
    """First fixture print: BTC-17JUN22-22000-C, iv 188.28 %, price 0.0305 BTC, index 20663.99."""
    t = json.loads((FIX / "deribit_history_trades_2026-10-14.json").read_text())["result"]["trades"][0]
    T = year_frac(EXP, t["timestamp"])
    F = implied_forward(t["price"], 22_000, T, t["iv"] / 100, True, t["index_price"])
    assert F is not None and 0.99 < F / t["index_price"] < 1.01  # June 2022: small backwardation


# ---------- surface ----------


def _trade(name, ts, F, idx, iv, mark_iv=None, direction="sell"):
    from tradedesk.analytics.instruments import parse_option_name

    on = parse_option_name(name)
    T = year_frac(int(on.expiry.timestamp() * 1000), ts)
    return {
        "instrument_name": name,
        "timestamp": ts,
        "iv": iv * 100,
        "price": inverse_price(F, on.strike, T, iv, on.is_call),
        "mark_price": inverse_price(F, on.strike, T, mark_iv if mark_iv is not None else iv, on.is_call),
        "index_price": idx,
        "direction": direction,
    }


def _synthetic_day(day=D0, spot=20_000.0, ratio=1.002, skew=True):
    """30-day expiry 2022-07-15 08:00; smile: higher IV at lower strikes when ``skew``."""
    ts = day + 9 * HOUR_MS
    trades = []
    for K in (14_000, 15_000, 16_000, 17_000, 18_000, 19_000, 20_000, 21_000, 22_000, 23_000, 24_000):
        iv = 0.60 + (0.004 * (20_000 - K) / 100 if skew else 0.0)
        for cp in ("P", "C"):
            for _ in range(3):
                trades.append(_trade(f"BTC-15JUL22-{K}-{cp}", ts, spot * ratio, spot, iv))
    return trades


def test_build_day_recovers_forward_and_marks():
    snap = build_day(_synthetic_day(), D0)
    e = int(EXP + 28 * DAY_MS)  # 2022-07-15 08:00
    assert snap is not None and e in snap["expiries"]
    x = snap["expiries"][e]
    assert x["ratio"] == pytest.approx(1.002, abs=1e-6)
    assert x["strikes"][17_000.0]["mark_iv"] == pytest.approx(0.60 + 0.12, abs=1e-5)
    assert x["strikes"][17_000.0]["n_put"] == 3
    assert x["T"] == pytest.approx((e - (D0 + 9 * HOUR_MS)) / (365 * DAY_MS))


def test_interpolation_and_rr25():
    snap = build_day(_synthetic_day(skew=False), D0)
    x = next(iter(snap["expiries"].values()))
    assert iv_at_strike(x, 17_500)[0] == pytest.approx(0.60, abs=1e-6)
    assert iv_at_strike(x, 17_500)[1] == "interp"
    assert rr25(x) == pytest.approx(0.0, abs=1e-6)
    skewed = next(iter(build_day(_synthetic_day(skew=True), D0)["expiries"].values()))
    assert rr25(skewed) < -0.02  # puts richer than calls
    assert iv_at_put_delta(skewed, -0.999) is None  # no extrapolation


def test_plan_entry_selects_25d_put_and_atm():
    p = plan_entry(build_day(_synthetic_day(), D0))
    assert p["covered"] and p["ka"] == 20_000.0
    d = black76_greeks(p["F"], p["kp"], p["T"], 0.60 + 0.004 * (20_000 - p["kp"]) / 100, False)["delta"]
    assert 0.20 <= -d <= 0.30
    assert p["dte"] == pytest.approx(30 - 1 / 24, abs=1e-6)


# ---------- v2 surface hygiene (review 2026-10-14, blockers 2-6) ----------

T9 = D0 + 9 * HOUR_MS
JUL = "BTC-15JUL22"  # 2022-07-15 is the last Friday of July 2022


def test_build_day_drops_prints_after_reference_time():
    """Blocker 4: a print after 09:00 with an absurd price/index must not change the snapshot."""
    base = [_trade(f"{JUL}-{K}-P", T9 - 600_000, 20_000, 20_000, 0.6) for K in (18_000, 20_000) for _ in range(3)]
    a = build_day(base, D0)
    late = _trade(f"{JUL}-18000-P", T9 + 60_000, 20_000, 99_000, 3.0, mark_iv=3.0)
    b = build_day(base + [late], D0)
    assert a == b
    v1 = build_day(base + [late], D0, **V1_BUILD)
    e = next(iter(a["expiries"]))
    assert a["expiries"][e]["strikes"][18_000.0]["n_put"] == 3
    assert v1["expiries"][e]["strikes"][18_000.0]["n_put"] == 4  # the v1 scheme did use it (documented flaw)


def test_spot_is_index_near_reference_time():
    """Blocker 2: spot = median index of the last 15 min before 09:00, independent of older prints."""
    old = [_trade(f"{JUL}-20000-P", T9 - k * HOUR_MS, 18_000, 18_000, 0.6) for k in (2, 3, 4, 5, 6, 7)]
    recent = [_trade(f"{JUL}-20000-C", T9 - 5 * 60_000, 21_000, 21_000, 0.6)]
    assert build_day(old + recent, D0)["spot"] == 21_000
    old2 = [dict(t, index_price=1_000.0 + i) for i, t in enumerate(old)]
    assert build_day(old2 + recent, D0)["spot"] == 21_000
    # nothing in the last 15 min: latest print's index
    assert build_day(old, D0)["spot"] == 18_000
    assert build_day(old + recent, D0, **V1_BUILD)["spot"] == 18_000  # v1: window median


def test_marks_use_freshest_prints_with_age_cap():
    """Blocker 3: mark IV from the latest prints <= 6 h old; stale-only strikes are dropped."""
    stale = [_trade(f"{JUL}-18000-P", T9 - 8 * HOUR_MS + i, 20_000, 20_000, 0.9) for i in range(20)]
    fresh = [_trade(f"{JUL}-18000-P", T9 - 30 * 60_000 + i, 20_000, 20_000, 0.6) for i in range(3)]
    only_stale = [_trade(f"{JUL}-16000-P", T9 - 7 * HOUR_MS, 20_000, 20_000, 0.9)]
    atm = [_trade(f"{JUL}-20000-C", T9 - 60_000, 20_000, 20_000, 0.6)]
    snap = build_day(stale + fresh + only_stale + atm, D0)
    st = next(iter(snap["expiries"].values()))["strikes"]
    assert st[18_000.0]["mark_iv"] == pytest.approx(0.6, abs=1e-5)
    assert st[18_000.0]["mark_age_h"] == pytest.approx((30 * 60_000 - 2) / HOUR_MS)
    assert st[18_000.0]["n_put"] == 23  # coverage counts every print in the window
    assert 16_000.0 not in st
    v1 = next(iter(build_day(stale + fresh + only_stale + atm, D0, **V1_BUILD)["expiries"].values()))["strikes"]
    assert v1[18_000.0]["mark_iv"] == pytest.approx(0.9, abs=1e-5)  # v1 took the oldest 15


def test_forward_ratio_uses_freshest_prints():
    """Blocker 3 (forward): F/S comes from near-the-money prints <= 6 h old, not from the whole 24 h window."""
    stale = [_trade(f"{JUL}-20000-P", T9 - 10 * HOUR_MS + i, 21_000, 20_000, 0.6) for i in range(30)]
    fresh = [_trade(f"{JUL}-20000-C", T9 - 20 * 60_000 + i, 20_040, 20_000, 0.6) for i in range(5)]
    x = next(iter(build_day(stale + fresh, D0)["expiries"].values()))
    assert x["ratio"] == pytest.approx(1.002, abs=1e-5)
    # no fresh near-the-money prints at all: fall back to every print of the window
    x_old = next(iter(build_day(stale, D0, max_mark_age_h=None)["expiries"].values()))
    assert x_old["ratio"] == pytest.approx(1.05, abs=1e-5)
    v1 = next(iter(build_day(stale + fresh, D0, **V1_BUILD)["expiries"].values()))
    assert v1["ratio"] == pytest.approx(1.05, abs=1e-5)  # v1 thinned evenly over the window


def test_dvol_control_uses_previous_day_close():
    """Blocker 5: the candle stamped day d closes at 24:00, so entry day d sees the d-1 candle."""
    dv = dvol_known_at_entry([(D0, 0.50), (D0 + DAY_MS, 0.70)])
    assert dv[D0 + DAY_MS] == 0.50 and dv[D0 + 2 * DAY_MS] == 0.70
    assert D0 not in dv


def _exp_snap(expiries):
    return {
        "day": D0,
        "t_ms": T9,
        "spot": 20_000.0,
        "expiries": {
            e: {
                "T": year_frac(e, T9),
                "F": 20_000.0,
                "ratio": 1.0,
                "n_prints": n,
                "strikes": {18_000.0: {"put_delta": -0.25, "n_put": w, "mark_iv": 0.6}},
            }
            for e, n, w in expiries
        },
    }


def test_expiry_rules():
    """Blocker 6: calendar rule picks the last-Friday expiry independent of flow."""
    from datetime import UTC, datetime

    def ms(y, m, d):
        return int(datetime(y, m, d, 8, tzinfo=UTC).timestamp() * 1000)

    assert is_monthly_expiry(ms(2022, 7, 29)) and is_monthly_expiry(ms(2022, 6, 24))
    assert not is_monthly_expiry(ms(2022, 7, 8)) and not is_monthly_expiry(ms(2022, 7, 22))
    weekly, monthly = ms(2022, 7, 15), ms(2022, 7, 29)  # 30 and 44 days from 2022-06-15
    snap = _exp_snap([(weekly, 10, 0), (monthly, 500, 40)])
    assert choose_expiry(snap, 30, 21, 56, rule="nearest") == weekly
    assert choose_expiry(snap, 30, 21, 56, rule="calendar_monthly") == monthly
    assert choose_expiry(snap, 30, 21, 56, rule="most_liquid_total") == monthly
    assert choose_expiry(snap, 30, 21, 56, rule="most_liquid_puts") == monthly
    assert choose_expiry(snap, 30, 21, 40, rule="calendar_monthly") is None
    with pytest.raises(ValueError):
        choose_expiry(snap, 30, 21, 56, rule="bogus")


# ---------- book P&L ----------


def _flat_days(n, spot, exp_ms, iv=0.6, K=20_000.0):
    days = {}
    for i in range(n):
        d = D0 + i * DAY_MS
        t = d + 9 * HOUR_MS
        T = year_frac(exp_ms, t)
        days[d] = {
            "day": d,
            "t_ms": t,
            "spot": spot[i],
            "expiries": {
                exp_ms: {"ratio": 1.0, "F": spot[i], "T": T, "strikes": {K: {"mark_iv": iv, "put_delta": 0.0, "n": 1, "n_put": 1}}}
            },
        }
    return days


def test_run_book_unhedged_short_put_is_premium_minus_payoff():
    exp = D0 + 3 * DAY_MS + 8 * HOUR_MS
    days = _flat_days(3, [20_000] * 3, exp)
    T0 = year_frac(exp, D0 + 9 * HOUR_MS)
    prem = black76_price(20_000, 20_000, T0, 0.6, False)
    r = run_book(days, D0, exp, [(20_000.0, False, -1.0, 0.6)], 18_000.0, option_fee=0, delivery_fee=0, hedge=False)
    assert r["pnl"] == pytest.approx((prem - 2_000) / 20_000, rel=1e-9)
    assert r["hedge_days"] == 3


def test_run_book_hedged_matches_hand_computation():
    exp = D0 + 2 * DAY_MS + 8 * HOUR_MS
    days = _flat_days(2, [20_000, 21_000], exp)
    t0, t1 = D0 + 9 * HOUR_MS, D0 + DAY_MS + 9 * HOUR_MS
    T0, T1 = year_frac(exp, t0), year_frac(exp, t1)
    d0 = black76_greeks(20_000, 20_000, T0, 0.6, False)["delta"]
    d1 = black76_greeks(21_000, 20_000, T1, 0.6, False)["delta"]
    prem = black76_price(20_000, 20_000, T0, 0.6, False)
    st = 20_500.0
    funding = [(t0 + HOUR_MS * h, 0.0001) for h in range(1, 48)]
    # short put => option delta -d (positive), hedge = +d (short perp: d < 0)
    h0, h1 = d0, d1
    hedge = h0 * 1_000 + h1 * (st - 21_000)
    fund = -h0 * 20_000 * funding_sum(funding, t0, t1) - h1 * 21_000 * funding_sum(funding, t1, exp)
    cost = 0.0005 * (abs(h0) * 20_000 + abs(h1 - h0) * 21_000 + abs(h1) * st)
    expected = prem - 0.0 + hedge + fund - cost
    r = run_book(days, D0, exp, [(20_000.0, False, -1.0, 0.6)], st, funding=funding, option_fee=0, delivery_fee=0)
    assert r["pnl"] == pytest.approx(expected / 20_000, rel=1e-9)
    assert funding_sum(funding, t0, t1) == pytest.approx(24 * 0.0001)


def test_run_book_ignores_data_after_expiry_and_respects_time():
    """Results for a trade must not change when data after its expiry changes."""
    exp = D0 + 3 * DAY_MS + 8 * HOUR_MS
    days = _flat_days(6, [20_000, 20_400, 19_700, 30_000, 5_000, 1_000], exp)
    a = run_book(days, D0, exp, [(20_000.0, False, -1.0, 0.6)], 19_900.0)
    days2 = dict(days)
    days2[D0 + 4 * DAY_MS] = dict(days[D0 + 4 * DAY_MS], spot=99_999.0)
    del days2[D0 + 5 * DAY_MS]
    assert run_book(days2, D0, exp, [(20_000.0, False, -1.0, 0.6)], 19_900.0) == a


def test_trailing_percentile_no_lookahead():
    s = [(i * DAY_MS, float(i % 7)) for i in range(200)]
    a = trailing_percentile(s, window_ms=90 * DAY_MS, min_obs=60)
    s2 = s[:150] + [(i * DAY_MS, -100.0) for i in range(150, 200)]
    b = trailing_percentile(s2, window_ms=90 * DAY_MS, min_obs=60)
    assert all(a[t] == b[t] for t in a if t < 150 * DAY_MS)
    assert min(a) == 60 * DAY_MS
    # increasing series: each new value is above all past ones
    inc = trailing_percentile([(i, float(i)) for i in range(10)], window_ms=100, min_obs=3)
    assert inc[5] == 1.0


def test_chain_reenters_on_expiry_day():
    entries = {D0 + i * DAY_MS: {"expiry": D0 + (i + 10) * DAY_MS + 8 * HOUR_MS} for i in range(0, 40) if i != 10}
    assert chain(entries, D0, D0 + 39 * DAY_MS) == [D0, D0 + 11 * DAY_MS, D0 + 21 * DAY_MS, D0 + 31 * DAY_MS]


def test_tercile_spread_and_bootstrap():
    rows = []
    for i in range(300):
        rr = (i % 3) / 3 + 0.01
        rows.append({"pnl": 1.0 if rr < 1 / 3 else (0.0 if rr >= 2 / 3 else 0.5), "rr_pct": rr, "dvol_pct": (i % 2) * 0.9})
    assert tercile_spread(rows) == pytest.approx(1.0)
    ci = block_bootstrap_ci(rows, tercile_spread, block=30, n_boot=200)
    assert ci == pytest.approx((1.0, 1.0))
    assert block_bootstrap_ci(rows[:10], tercile_spread) is None
    assert math.isfinite(ci[0])


# ---------- provider ----------


def _deribit(handler) -> Deribit:
    return Deribit(Http("deribit_history", HISTORY_URL, transport=httpx.MockTransport(handler), retries=0))


def test_provider_trades_by_time_parses_fixture():
    body = json.loads((FIX / "deribit_history_trades_2026-10-14.json").read_text())
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        seen["path"] = req.url.path
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": body["result"]})

    dr = _deribit(h)
    f = asyncio.run(dr.last_trades_by_currency_and_time("btc", "option", 1655280000000, 1655287200000, count=5))
    assert seen["path"].endswith("/public/get_last_trades_by_currency_and_time")
    assert seen["currency"] == "BTC" and seen["sorting"] == "asc" and seen["count"] == "5"
    assert f.data["has_more"] is True and len(f.data["trades"]) == 5
    assert f.data["trades"][0]["iv"] == pytest.approx(188.28)
    g = asyncio.run(dr.last_trades_by_instrument_and_time("BTC-17JUN22-22000-C", 0, 1))
    assert seen["instrument_name"] == "BTC-17JUN22-22000-C" and len(g.data["trades"]) == 5


def test_provider_delivery_and_funding_fixtures():
    dp = json.loads((FIX / "deribit_delivery_prices_2026-10-14.json").read_text())["result"]
    fr = json.loads((FIX / "deribit_funding_history_2026-10-14.json").read_text())["result"]

    def h(req: httpx.Request) -> httpx.Response:
        res = dp if req.url.path.endswith("get_delivery_prices") else fr
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": res})

    dr = _deribit(h)
    d = asyncio.run(dr.delivery_prices("btc_usd", count=3)).data
    assert d["records_total"] == 2637 and d["data"][0] == {"date": "2026-10-13", "delivery_price": 83826.89}
    f = asyncio.run(dr.funding_rate_history("BTC-PERPETUAL", 0, 1)).data
    assert len(f) == 3 and f[0]["interest_1h"] < 0


def test_provider_error_maps_to_provider_error():
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 10000, "message": "Bad request"}})

    with pytest.raises(ProviderError) as e:
        asyncio.run(_deribit(h).delivery_prices("btc_usd"))
    assert e.value.reason == "backend_error"
