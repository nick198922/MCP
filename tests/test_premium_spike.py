"""Perp-spot premium spike convergence: tickers, filters, signals, trade simulation, archive samples."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.premium_spike import (
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    Bars,
    funding_covered,
    liq_move,
    price_ratio,
    round_trip_cost,
    run_signals,
    select_events,
    simulate,
    slippage,
    spot_candidates,
    top_cluster_drop,
    trailing_turnover,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_vision import BinanceVision, parse_klines_zip

FIX = Path(__file__).parent / "fixtures"
T0 = 28_333_334 * MIN_MS  # a whole minute


def m(k: int) -> int:
    return T0 + k * MIN_MS


# ---------------------------------------------------------------- tickers and filters


def test_spot_candidates_multiplier_and_same_ticker():
    spot = {"PEPEUSDT", "1000SATSUSDT", "SATSUSDT", "ARBUSDT"}
    assert spot_candidates("1000PEPEUSDT", spot) == [("PEPEUSDT", 1000)]
    assert spot_candidates("1000SATSUSDT", spot) == [("1000SATSUSDT", 1), ("SATSUSDT", 1000)]
    assert spot_candidates("ARBUSDT", spot) == [("ARBUSDT", 1)]
    assert spot_candidates("1MBABYDOGEUSDT", {"BABYDOGEUSDT"}) == [("BABYDOGEUSDT", 1_000_000)]
    assert spot_candidates("XYZUSDT", spot) == []


def test_price_ratio_and_turnover_read_only_prior_days():
    perp = {d: 1000.0 * (1 + 0.001 * d) for d in range(0, 40)}
    spot = {d: 1.0 * (1 + 0.001 * d) for d in range(0, 40)}
    assert price_ratio(perp, spot, 1000, 35) == pytest.approx(1.0)
    assert price_ratio(perp, spot, 1, 35) == pytest.approx(1000.0)
    spot2 = dict(spot)
    spot2[35] = 99.0  # the event day itself and later days do not count
    spot2[36] = 99.0
    assert price_ratio(perp, spot2, 1000, 35) == pytest.approx(1.0)
    assert price_ratio(perp, spot, 1000, 0) is None
    qv = {d: float(d) for d in range(5, 40)}
    med, n = trailing_turnover(qv, 35)
    assert n == 30 and med == pytest.approx(19.5)  # days 5..34
    qv[35] = 1e12
    assert trailing_turnover(qv, 35) == (med, n)
    assert trailing_turnover({}, 10) == (None, 0)


def test_slippage_tiers_and_cost():
    assert slippage(60e6) == 0.0005 and slippage(50e6) == 0.0005
    assert slippage(10e6) == 0.0010 and slippage(1e6) == 0.0020 and slippage(None) == 0.0020
    # fees 2*0.10% + 2*0.05% = 0.30 %, slippage 2 sides x (0.10 % + 0.05 %) = 0.30 %
    assert round_trip_cost(0.001, 0.0005) == pytest.approx(0.006)
    assert round_trip_cost(0.001, 0.0005, mult=2.0) == pytest.approx(0.012)
    assert round_trip_cost(0.001, 0.0005, slip_mult=3.0) == pytest.approx(0.003 + 0.009)
    assert liq_move(2.0) == pytest.approx(0.475) and liq_move(1.0) == pytest.approx(0.975)


# ---------------------------------------------------------------- signals


def test_run_signals_fresh_contiguous_runs():
    prem = {m(0): 0.005, m(1): 0.011, m(2): 0.012, m(3): 0.013, m(4): 0.004, m(5): 0.02, m(7): 0.02, m(8): 0.03}
    # run completes at m(2); m(3) continues it; m(5)-m(7) is not contiguous (m(6) missing); m(7)+m(8) is fresh
    assert run_signals(prem, m(0), m(10)) == [m(2), m(8)]
    assert run_signals(prem, m(0), m(10), lo=0.011, hi=0.0125) == [m(2)]
    neg = {t: -v for t, v in prem.items()}
    assert run_signals(neg, m(0), m(10), side=-1) == [m(2), m(8)]
    assert run_signals(prem, m(0), m(10), run=3) == [m(3)]


def test_select_events_cooldown_and_exit():
    calls = []

    def sim(s: int):
        calls.append(s)
        return None, "x"

    out = select_events([m(0), m(10), 6 * HOUR_MS + m(0), 6 * HOUR_MS + m(1)], sim)
    assert [s for s, _, _ in out] == [m(0), m(0) + 6 * HOUR_MS]
    assert calls == [m(0), m(0) + 6 * HOUR_MS]


# ---------------------------------------------------------------- trades


def _market(liq_high: float | None = None, mae_spike: float | None = None):
    prem = {m(0): 0.0, m(1): 0.012, m(2): 0.013, m(3): 0.008, m(4): 0.005, m(5): 0.001, m(6): 0.0005, m(7): 0.0}
    if mae_spike is not None:
        prem[m(3)] = mae_spike
    perp_open = {3: 101.0, 4: 100.99, 5: 100.5, 6: 100.3, 7: 100.2}
    spot_open = {3: 100.0, 4: 100.0, 5: 100.05, 6: 100.1, 7: 100.1}
    perp = Bars({m(k): (o, o + 0.1, o - 0.1, o, 10.0) for k, o in perp_open.items()})
    perp.rows[m(2)] = (100.9, 101.0, 100.8, 100.95, 10.0)
    if liq_high is not None:
        o = perp_open[4]
        perp.rows[m(4)] = (o, liq_high, o - 0.1, o, 10.0)
    spot = Bars({m(k): (o, o + 0.1, o - 0.1, o, 5.0) for k, o in spot_open.items()})
    spot.rows[m(2)] = (99.9, 100.0, 99.8, 99.95, 5.0)
    funding = [[m(-60 * 4), 8, 0.0001], [m(4), 8, 0.0005], [m(4) + 8 * HOUR_MS, 8, 0.0001]]
    return prem, perp, spot, funding


def test_simulate_analytic_trade():
    prem, perp, spot, funding = _market()
    tr, why = simulate(prem, perp, spot, funding, m(2))
    assert why == "" and tr is not None
    assert tr.entry_ms == m(3) and tr.exit_ms == m(6) and tr.converged  # p(m5) <= 0.2 % -> exit at the open of m6
    assert tr.p_in == 101.0 and tr.s_in == 100.0 and tr.p_out == 100.3 and tr.s_out == 100.1
    basis = -(100.3 / 101.0 - 1) + (100.1 / 100.0 - 1)
    assert tr.basis_pnl == pytest.approx(basis)
    assert tr.funding_pnl() == pytest.approx(0.0005 * 100.99 / 101.0)
    assert tr.mae == 0.0 and tr.prem_signal == 0.013
    assert tr.liq_ms == {1.0: None, 2.0: None, 3.0: None}
    assert tr.funding_ok
    cost = round_trip_cost(0.001, 0.0005)
    assert tr.net(2.0, cost) == pytest.approx(basis + 0.0005 * 100.99 / 101.0 - cost)
    assert tr.hold_ms == 3 * MIN_MS


def test_simulate_liquidation_keeps_spot_leg_and_stops_funding():
    prem, perp, spot, funding = _market(liq_high=101.0 * 1.475 + 0.01)
    tr, _ = simulate(prem, perp, spot, funding, m(2))
    assert tr.liq_ms[2.0] == m(4) and tr.liq_ms[1.0] is None and tr.liq_ms[3.0] == m(4)
    # perp margin lost (-1/2), spot leg to the regular exit, funding at m(4) not received (settled at the liquidation bar)
    assert tr.net(2.0, 0.0) == pytest.approx(-0.5 + (100.1 / 100.0 - 1))
    assert tr.net(1.0, 0.0) == pytest.approx(tr.basis_pnl + tr.funding_pnl())


def test_simulate_mae_timeout_delay_and_same_bar():
    prem, perp, spot, funding = _market(mae_spike=0.05)
    tr, _ = simulate(prem, perp, spot, funding, m(2))
    assert tr.mae == pytest.approx(0.05 - 0.013)
    tr, _ = simulate(prem, perp, spot, funding, m(2), max_hold_ms=2 * MIN_MS)
    assert not tr.converged and tr.exit_ms == m(5) and tr.p_out == 100.5
    tr, _ = simulate(prem, perp, spot, funding, m(2), delay_bars=2)
    assert tr.entry_ms == m(4) and tr.p_in == 100.99
    tr, _ = simulate(prem, perp, spot, funding, m(2), same_bar=True)
    assert tr.entry_ms == m(3) and tr.p_in == 100.95 and tr.s_in == 99.95
    # no traded bar at entry -> untradable
    spot.rows[m(3)] = (100.0, 100.0, 100.0, 100.0, 0.0)
    assert simulate(prem, perp, spot, funding, m(2)) == (None, "no traded entry bar")


def test_short_discount_side_mirror():
    prem = {k: -v for k, v in _market()[0].items()}
    _, perp, spot, funding = _market()
    tr, _ = simulate(prem, perp, spot, funding, m(2), side=-1)
    assert tr.exit_ms == m(6)
    assert tr.basis_pnl == pytest.approx((100.3 / 101.0 - 1) - (100.1 / 100.0 - 1))
    assert tr.funding_pnl() == pytest.approx(-0.0005 * 100.99 / 101.0)  # the long perp pays a positive rate


def test_no_lookahead_after_exit_and_before_signal():
    prem, perp, spot, funding = _market()
    base, _ = simulate(prem, perp, spot, funding, m(2))
    sig = run_signals(prem, m(0), m(8))
    # rewrite everything after the exit: premium, bars, funding
    for k in range(7, 400):
        prem[m(k)] = 0.5
        perp.rows[m(k)] = (999.0, 9999.0, 1.0, 999.0, 1.0)
        spot.rows[m(k)] = (1.0, 2.0, 0.5, 1.0, 1.0)
    funding.append([m(30), 8, 0.9])
    again, _ = simulate(prem, perp, spot, funding, m(2))
    assert (again.p_out, again.s_out, again.exit_ms, again.funding_pnl(), again.mae, again.liq_ms) == (
        base.p_out,
        base.s_out,
        base.exit_ms,
        base.funding_pnl(),
        base.mae,
        base.liq_ms,
    )
    assert run_signals(prem, m(0), m(7)) == [s for s in sig if s < m(7)]


def test_funding_covered_holes():
    recs = [[0, 8, 0.0], [8 * HOUR_MS, 8, 0.0], [16 * HOUR_MS, 8, 0.0], [24 * HOUR_MS, 8, 0.0]]
    assert funding_covered(recs, HOUR_MS, 20 * HOUR_MS)
    holed = [recs[0], recs[2], recs[3]]
    assert not funding_covered(holed, HOUR_MS, 20 * HOUR_MS)
    assert funding_covered(holed, 17 * HOUR_MS, 20 * HOUR_MS)  # the hole is before the window
    assert not funding_covered(recs, -HOUR_MS, 2 * HOUR_MS)  # no settlement before the entry
    assert funding_covered(recs, 25 * HOUR_MS, 30 * HOUR_MS)  # series ends inside one interval of the exit
    assert not funding_covered(recs, 25 * HOUR_MS, 40 * HOUR_MS)
    assert not funding_covered([], 0, 1)
    four = [[k * 4 * HOUR_MS, 4, 0.0] for k in range(10)]
    assert funding_covered(four, HOUR_MS, 30 * HOUR_MS)


def test_top_cluster_drop():
    assert top_cluster_drop([5.0, 1.0, 2.0, -1.0], ["a", "b", "a", "c"], 1) == [1.0, -1.0]
    assert top_cluster_drop([5.0, 1.0], ["a", "b"], 0) == [5.0, 1.0]


# ---------------------------------------------------------------- archive samples (data.binance.vision, fetched 2026-10-15)


def test_real_premium_index_matches_perp_spot_basis():
    pi = parse_klines_zip((FIX / "binance_vision_ARBUSDT-1m-2024-03-12_pi1m_d_2026-10-15.zip").read_bytes())
    pk = parse_klines_zip((FIX / "binance_vision_ARBUSDT-1m-2024-03-12_pk1m_d_2026-10-15.zip").read_bytes())
    sk = parse_klines_zip((FIX / "binance_vision_ARBUSDT-1m-2024-03-12_sk1m_d_2026-10-15.zip").read_bytes())
    assert len(pi) == len(pk) == len(sk) == 1440
    assert pi[0][0] == 1_710_201_600_000 and pi[0][4] == pytest.approx(0.00191896)
    assert pi[0][5] == 0.0  # premium klines carry no volume
    p = {int(r[0]): r[4] for r in pi}
    s = {int(r[0]): r[4] for r in sk}
    diffs = sorted(abs(r[4] / s[int(r[0])] - 1 - p[int(r[0])]) for r in pk)
    assert diffs[len(diffs) // 2] < 0.0003  # premium index is a decimal close to perp/spot - 1 (median 0.7 bp)
    h1 = parse_klines_zip((FIX / "binance_vision_ARBUSDT-1h-2024-03_pi1h_m_2026-10-15.zip").read_bytes())
    hour = {int(r[0]): r[2] for r in h1}
    for r in pi:  # 1h high bounds every 1m high: the prescreen is lossless
        assert r[2] <= hour[int(r[0]) // HOUR_MS * HOUR_MS] + 1e-12
    d1 = parse_klines_zip((FIX / "binance_vision_ARBUSDT-1d-2024-03_sk1d_m_2026-10-15.zip").read_bytes())
    assert len(d1) == 31 and d1[0][4] == pytest.approx(1.988) and d1[0][6] > 0


async def test_provider_premium_and_spot_paths():
    blob_pi = (FIX / "binance_vision_ARBUSDT-1m-2024-03-12_pi1m_d_2026-10-15.zip").read_bytes()
    blob_sk = (FIX / "binance_vision_ARBUSDT-1m-2024-03-12_sk1m_d_2026-10-15.zip").read_bytes()
    blob_h = (FIX / "binance_vision_ARBUSDT-1h-2024-03_pi1h_m_2026-10-15.zip").read_bytes()
    blob_d = (FIX / "binance_vision_ARBUSDT-1d-2024-03_sk1d_m_2026-10-15.zip").read_bytes()
    routes = {
        "/data/futures/um/daily/premiumIndexKlines/ARBUSDT/1m/ARBUSDT-1m-2024-03-12.zip": blob_pi,
        "/data/futures/um/monthly/premiumIndexKlines/ARBUSDT/1h/ARBUSDT-1h-2024-03.zip": blob_h,
        "/data/spot/daily/klines/ARBUSDT/1m/ARBUSDT-1m-2024-03-12.zip": blob_sk,
        "/data/spot/monthly/klines/ARBUSDT/1d/ARBUSDT-1d-2024-03.zip": blob_d,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path in routes:
            return httpx.Response(200, content=routes[req.url.path])
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    assert len((await v.um_premium_index_daily("ARBUSDT", "1m", "2024-03-12")).data) == 1440
    assert len((await v.um_premium_index_monthly("ARBUSDT", "1h", 2024, 3)).data) == 744
    assert len((await v.spot_klines_daily("ARBUSDT", "1m", "2024-03-12")).data) == 1440
    got = await v.spot_klines_monthly("ARBUSDT", "1d", 2024, 3)
    assert len(got.data) == 31 and got.evidence.provider == "binance_vision"
    with pytest.raises(ProviderError) as e:
        await v.spot_klines_daily("ARBUSDT", "1m", "2024-03-13")
    assert e.value.reason == "data_unavailable"
    with pytest.raises(ProviderError):
        await v.um_premium_index_daily("ARBUSDT", "1m", "2024/03/12")
    with pytest.raises(ProviderError):
        await v.spot_klines_monthly("../x", "1d", 2024, 3)
    await v.aclose()


def _listing_xml(dirs: list[str], next_marker: str | None) -> bytes:
    trunc = "true" if next_marker else "false"
    nm = f"<NextMarker>{next_marker}</NextMarker>" if next_marker else ""
    cps = "".join(f"<CommonPrefixes><Prefix>{d}</Prefix></CommonPrefixes>" for d in dirs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>data.binance.vision</Name><IsTruncated>{trunc}</IsTruncated>{nm}{cps}</ListBucketResult>"
    ).encode()


async def test_spot_symbols_walks_two_pages():
    pre = "data/spot/monthly/klines/"
    page1 = _listing_xml([f"{pre}ARBUSDT/", f"{pre}BTCUSDT/"], f"{pre}BTCUSDT/")
    page2 = _listing_xml([f"{pre}PEPEUSDT/", f"{pre}ARBUSDT/"], None)  # duplicate is de-duplicated
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        seen.append(params)
        assert req.url.path == "/data.binance.vision" and params["prefix"] == pre and params["delimiter"] == "/"
        return httpx.Response(200, content=page2 if params.get("marker") else page1)

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    got = await v.spot_symbols()
    assert got.data == ["ARBUSDT", "BTCUSDT", "PEPEUSDT"]
    assert len(seen) == 2 and "marker" not in seen[0] and seen[1]["marker"] == f"{pre}BTCUSDT/"
    await v.aclose()


def test_day_constant():
    assert DAY_MS == 24 * HOUR_MS
