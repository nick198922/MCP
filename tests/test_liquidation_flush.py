"""Liquidation-flush study: pure analytics (analytic references) and the data.binance.vision provider (real samples)."""

from __future__ import annotations

import math
import random
from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.liquidation_flush import (
    DAY_MS,
    FIVE_MIN_MS,
    HOUR_MS,
    MAD_SCALE,
    MIN_MS,
    base_of,
    cluster_bootstrap_ci,
    cluster_bootstrap_p,
    control_times,
    detect_flush_events,
    held_extreme,
    holm,
    month_starts,
    monthly_universe,
    robust_z,
    simulate_trade,
    summarise,
    symbol_eligible,
    window_return,
    without_top_days,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_vision import BinanceVision, parse_klines_zip, parse_listing

FIX = Path(__file__).parent / "fixtures"
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC


def series(n: int, *, seed: int = 1) -> tuple[list[int], list[float], list[float]]:
    """Flat-ish price with small noise and volume alternating 100/110 (median 105, MAD 5)."""
    rng = random.Random(seed)
    ts = [T0 + i * FIVE_MIN_MS for i in range(n)]
    close = [100.0 * (1 + rng.uniform(-0.001, 0.001)) for _ in range(n)]
    qv = [100.0 if i % 2 else 110.0 for i in range(n)]
    return ts, close, qv


# ---------------------------------------------------------------- robust z / events


def test_robust_z_analytic():
    assert robust_z([1, 2, 3, 4, 5], 10) == pytest.approx(7 / MAD_SCALE)
    assert robust_z([5, 5, 5], 9) is None  # MAD = 0
    assert robust_z([], 1) is None


def test_event_long_after_crash_with_volume_spike():
    ts, close, qv = series(2100)
    i = 2050
    close[i] = close[i - 1] * 0.95
    qv[i] = 1000.0
    ev = detect_flush_events(ts, close, qv)
    assert len(ev) == 1
    e = ev[0]
    assert e["ts"] == ts[i] and e["signal_ts"] == ts[i] + FIVE_MIN_MS and e["direction"] == 1
    # window = 2016 bars strictly before i: half 100, half 110 -> median 105, MAD 5
    assert e["z"] == pytest.approx((1000 - 105) / (MAD_SCALE * 5))
    assert e["ret"] == pytest.approx(-0.05)


def test_event_short_after_squeeze_and_threshold_edges():
    ts, close, qv = series(2100)
    close[2050] = close[2049] * 1.05
    qv[2050] = 1000.0
    assert detect_flush_events(ts, close, qv)[0]["direction"] == -1
    # volume z just below 5 -> no event
    qv[2050] = 105 + 4.99 * MAD_SCALE * 5
    assert detect_flush_events(ts, close, qv) == []
    # return just below 4 % -> no event
    ts, close, qv = series(2100)
    close[2050] = close[2049] * 0.9601
    qv[2050] = 1000.0
    assert detect_flush_events(ts, close, qv) == []


def test_event_needs_contiguous_previous_bar_and_history():
    ts, close, qv = series(2100)
    close[2050] = close[2049] * 0.95
    qv[2050] = 1000.0
    ts2 = ts[:2049] + ts[2050:]  # drop bar 2049: previous bar is 10 min earlier
    c2 = close[:2049] + close[2050:]
    q2 = qv[:2049] + qv[2050:]
    assert detect_flush_events(ts2, c2, q2) == []
    ts, close, qv = series(1400)  # < 1500 bars of history
    close[1390] = close[1389] * 0.95
    qv[1390] = 1000.0
    assert detect_flush_events(ts, close, qv) == []


def test_cooldown_and_eligibility():
    ts, close, qv = series(2300)
    for i in (2050, 2050 + 12, 2050 + 48):  # +1h (inside 4h cooldown), +4h (allowed)
        close[i] = close[i - 1] * 0.95
        qv[i] = 1000.0
    ev = detect_flush_events(ts, close, qv)
    assert [e["ts"] for e in ev] == [ts[2050], ts[2050 + 48]]
    # an ineligible first event does not start a cooldown
    ev = detect_flush_events(ts, close, qv, eligible=lambda t: t != ts[2050])
    assert [e["ts"] for e in ev] == [ts[2062]]


def test_events_before_t_do_not_depend_on_later_data():
    rng = random.Random(7)
    n = 6000
    ts = [T0 + i * FIVE_MIN_MS for i in range(n)]
    close, qv, p = [], [], 100.0
    for _ in range(n):
        shock = rng.random() < 0.004
        p *= 1 + (rng.choice([-0.06, 0.06]) if shock else rng.gauss(0, 0.004))
        close.append(p)
        qv.append(rng.lognormvariate(5, 0.5) * (20 if shock else 1))
    base = detect_flush_events(ts, close, qv)
    assert base, "synthetic series should contain events"
    cut = ts[4500]
    close2 = close[:4500] + [x * rng.uniform(0.5, 1.5) for x in close[4500:]]
    qv2 = qv[:4500] + [x * 50 for x in qv[4500:]]
    other = detect_flush_events(ts, close2, qv2)
    assert [e for e in base if e["ts"] < cut] == [e for e in other if e["ts"] < cut]


# ---------------------------------------------------------------- universe


def test_month_starts_and_symbol_rules():
    ms = month_starts(T0 + 5 * DAY_MS, T0 + 70 * DAY_MS)
    assert ms == [T0, T0 + 31 * DAY_MS, T0 + 60 * DAY_MS]  # Jan, Feb (2024 leap), Mar
    assert base_of("1000PEPEUSDT") == "PEPE" and base_of("1MBABYDOGEUSDT") == "BABYDOGE" and base_of("1INCHUSDT") == "1INCH"
    assert symbol_eligible("SOLUSDT", "COIN")
    assert not symbol_eligible("XAUUSDT", "COMMODITY")
    assert not symbol_eligible("BTCUSDT_250328", "COIN")
    assert not symbol_eligible("USDCUSDT", None) and not symbol_eligible("BTCDOMUSDT", None)
    assert symbol_eligible("FTTUSDT", None) and symbol_eligible("币安人生USDT", "COIN")


def test_monthly_universe_is_point_in_time():
    month = T0 + 31 * DAY_MS  # 2024-02-01
    days = [month - (k + 1) * DAY_MS for k in range(30)]
    daily = {
        "A": [(d, 10.0) for d in days],
        "B": [(d, 5.0) for d in days],
        "C": [(d, 100.0) for d in days[:10]],  # only 10 days of history -> excluded
    }
    assert monthly_universe(daily, month, top_n=2) == ["A", "B"]
    # data on/after the month start must not change the ranking
    daily["B"] = daily["B"] + [(month + k * DAY_MS, 1e9) for k in range(5)]
    assert monthly_universe(daily, month, top_n=1) == ["A"]


# ---------------------------------------------------------------- trades


def flat_m1(start: int, n: int, px: float = 100.0) -> dict[int, tuple[float, float, float, float]]:
    return {start + k * MIN_MS: (px, px, px, px) for k in range(n)}


def test_simulate_trade_analytic_long_short_costs():
    m1 = flat_m1(T0, 200)
    entry_ts = T0 + MIN_MS  # close of the bar opened at T0
    m1[entry_ts + 2 * HOUR_MS - MIN_MS] = (101, 103, 101, 102.0)
    long = simulate_trade(m1, entry_ts, 1, 2 * HOUR_MS, cost=0.003)
    assert long["gross"] == pytest.approx(0.02) and long["net"] == pytest.approx(0.017)
    short = simulate_trade(m1, entry_ts, -1, 2 * HOUR_MS, cost=0.003)
    assert short["gross"] == pytest.approx(-0.02) and short["net"] == pytest.approx(-0.023)
    assert short["mae"] == pytest.approx(-0.03) and long["mfe"] == pytest.approx(0.03)
    # worst entry: high of the entry bar for a long
    m1[T0] = (100, 101, 99, 100)
    w = simulate_trade(m1, entry_ts, 1, 2 * HOUR_MS, cost=0.0, worst_entry=True)
    assert w["entry"] == 101 and w["gross"] == pytest.approx(102 / 101 - 1)


def test_simulate_trade_stop_and_gap_and_missing():
    m1 = flat_m1(T0, 200)
    entry_ts = T0 + MIN_MS
    m1[entry_ts + 10 * MIN_MS] = (99, 99, 96, 98)  # touches -3 % intrabar -> filled at the stop level
    r = simulate_trade(m1, entry_ts, 1, 2 * HOUR_MS, cost=0.0, stop=0.03)
    assert r["stopped"] and r["exit"] == pytest.approx(97.0) and r["gross"] == pytest.approx(-0.03)
    m1[entry_ts + 10 * MIN_MS] = (95, 96, 94, 95)  # opens beyond the stop -> filled at the open
    r = simulate_trade(m1, entry_ts, 1, 2 * HOUR_MS, cost=0.0, stop=0.03)
    assert r["exit"] == 95 and r["gross"] == pytest.approx(-0.05)
    del m1[entry_ts + 2 * HOUR_MS - MIN_MS]
    assert simulate_trade(m1, entry_ts, 1, 2 * HOUR_MS) is None
    m1 = flat_m1(T0, 200)
    for k in range(10, 17):
        del m1[entry_ts + k * MIN_MS]
    assert simulate_trade(m1, entry_ts, 1, 2 * HOUR_MS, max_missing=5) is None


def test_trade_does_not_see_bars_after_exit():
    m1 = flat_m1(T0, 400)
    entry_ts = T0 + MIN_MS
    a = simulate_trade(m1, entry_ts, 1, HOUR_MS, stop=0.03)
    for k in range(61, 400):
        m1[T0 + k * MIN_MS] = (1.0, 1.0, 1.0, 1.0)
    assert simulate_trade(m1, entry_ts, 1, HOUR_MS, stop=0.03) == a


def test_held_extreme_and_window_return():
    m1 = flat_m1(T0, 10)
    assert held_extreme(m1, T0, 5, 1, 99.0) is True
    m1[T0 + 2 * MIN_MS] = (100, 100, 98.5, 100)
    assert held_extreme(m1, T0, 5, 1, 99.0) is False
    assert held_extreme(m1, T0, 20, 1, 50.0) is None
    assert window_return({1: 100.0, 2: 103.0}, 1, 2) == pytest.approx(0.03)
    assert window_return({1: 100.0}, 1, 2) is None


# ---------------------------------------------------------------- controls and statistics


def test_control_times_same_minute_deterministic_and_blocked():
    sig = T0 + 37 * MIN_MS
    a = control_times(sig, "SOLUSDT", k=3)
    assert a == control_times(sig, "SOLUSDT", k=3) and len(a) == 3
    assert all((t - sig) % DAY_MS == 0 and t != sig and abs(t - sig) <= 14 * DAY_MS for t in a)
    b = control_times(sig, "SOLUSDT", k=3, blocked=lambda t: t == a[0])
    assert a[0] not in b and len(b) == 3


def test_cluster_bootstrap_and_holm():
    vals = [0.01] * 30
    days = [i // 3 for i in range(30)]
    lo, hi = cluster_bootstrap_ci(vals, days, n_boot=500)
    assert lo == pytest.approx(0.01) and hi == pytest.approx(0.01)
    assert cluster_bootstrap_ci([1.0, 2.0], [0, 0]) is None
    # one huge day dominates: day-clustered CI is much wider than if the 10 copies were independent
    vals = [0.0] * 90 + [1.0] * 10
    days = list(range(90)) + [99] * 10
    lo, hi = cluster_bootstrap_ci(vals, days, n_boot=2000)
    assert lo == 0.0 and hi > 0.1
    assert cluster_bootstrap_p([0.01] * 20, list(range(20)), n_boot=500) == pytest.approx(1 / 501)
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def test_without_top_days_and_summarise():
    v, d, drop = without_top_days([5.0, 1.0, 1.0, -1.0, 3.0], ["a", "b", "b", "c", "d"], 2)
    assert drop == ["a", "d"] and v == [1.0, 1.0, -1.0]
    s = summarise([0.01, 0.03, -0.01, 0.05], ["x", "y", "z", "w"], n_boot=500)
    assert s["n"] == 4 and s["days"] == 4 and s["mean"] == pytest.approx(0.02) and s["median"] == pytest.approx(0.02)
    assert s["win"] == 0.75 and s["ci"][0] < 0.02 < s["ci"][1]
    assert summarise([], []) == {"n": 0, "days": 0}


# ---------------------------------------------------------------- data.binance.vision provider (real samples)


def test_parse_klines_zip_real_samples():
    # Archive sample fetched 2026-10-14: FTT perp (delisted after Nov 2022 collapse), November 2022, daily bars.
    rows = parse_klines_zip((FIX / "binance_vision_FTTUSDT-1d-2022-11_2026-10-14.zip").read_bytes())
    assert len(rows) == 30 and rows[0][0] == 1_667_260_800_000  # 2022-11-01
    assert rows[0][1:5] == [26.077, 26.418, 25.52, 25.83]
    assert min(r[3] for r in rows) == pytest.approx(1.2158)
    # BTCUSDT perp 5m on 2025-10-10: the cascade low 101 516.5 at 21:20 UTC; quote volume = base x price
    rows = parse_klines_zip((FIX / "binance_vision_BTCUSDT-5m-2025-10-10_2026-10-14.zip").read_bytes())
    assert len(rows) == 288
    low = min(rows, key=lambda r: r[3])
    assert low[0] == 1_760_131_200_000 and low[3] == 101_516.5
    ratios = sorted(r[6] / (r[5] * r[4]) for r in rows)
    assert ratios[len(ratios) // 2] == pytest.approx(1.0, abs=0.005)


def test_parse_listing_real_sample():
    dirs, keys, nxt = parse_listing((FIX / "binance_vision_listing_FTTUSDT_1d_2026-10-14.xml").read_bytes())
    assert dirs == [] and nxt is None
    assert "data/futures/um/monthly/klines/FTTUSDT/1d/FTTUSDT-1d-2022-11.zip" in keys


async def test_provider_monthly_klines_and_404():
    blob = (FIX / "binance_vision_FTTUSDT-1d-2022-11_2026-10-14.zip").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("FTTUSDT-1d-2022-11.zip"):
            return httpx.Response(200, content=blob)
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    f = await v.um_klines_monthly("FTTUSDT", "1d", 2022, 11)
    assert len(f.data) == 30 and f.evidence.provider == "binance_vision"
    with pytest.raises(ProviderError) as e:
        await v.um_klines_monthly("FTTUSDT", "1d", 2026, 9)
    assert e.value.reason == "data_unavailable"
    with pytest.raises(ProviderError):
        await v.um_klines_monthly("../x", "1d", 2022, 11)
    await v.aclose()
    assert not math.isnan(f.data[0][0])
