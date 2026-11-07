"""Direct tests for twelve analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 209).

They feed the strategy-sieve specs (Wilder's DMI / ADX in five copies: `adx_di_cross.dmi`, `holy_grail_pullback.dmi`,
`stochastic_pop.dmi`, `freqtrade_adx_sma_cross.adx_wilder`, `hlhb_rsi_ema_adx.adx_wilder`; `kst_signal_cross.sma_valid`,
`tsi_zero_cross.ema_valid`), the demo trader's live S3 / S16 scan (`setup_signals.range_setups`), the news-events and
perp-launch reports (`news_events.perp_symbols`), the pre-market fade report (`premarket_fade.bars_from_rows`), the NY
midnight-open report (`ny_midnight_open.ny_days`) and the cross-sectional factor panel (`xs_factors.oriented_signal`).
References are hand-computed values of the published formulas (Wilder 1978: DI from sums of 14 periods, then
prior - prior / n + current, ADX = mean of the first n DX, then (prior x (n - 1) + DX) / n), real samples from
tests/fixtures/ (Binance spot daily and USD-M 1h klines), real exchange announcement titles and calendar facts; no output
of the code under test is reused as an expected value.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.news_events import classify_delisting, perp_base, perp_symbols
from tradedesk.analytics.ny_midnight_open import ny_days, ny_ms
from tradedesk.analytics.premarket_fade import bars_from_rows, entry_point
from tradedesk.analytics.sieve_specs import adx_di_cross, freqtrade_adx_sma_cross, hlhb_rsi_ema_adx, holy_grail_pullback, stochastic_pop
from tradedesk.analytics.sieve_specs.kst_signal_cross import sma_valid
from tradedesk.analytics.sieve_specs.tsi_zero_cross import ema_valid
from tradedesk.analytics.xs_factors import oriented_signal, rank_ic

FIX = Path(__file__).parent / "fixtures"
HOUR = 3_600_000
H4 = 4 * HOUR
DAY = 24 * HOUR

DMI = (adx_di_cross.dmi, holy_grail_pullback.dmi, stochastic_pop.dmi)


def _adx_only(n: int):
    """The five ADX copies as one call shape b, pos -> ADX (the two adx_wilder take (n, pos) / (pos, n))."""
    return (
        lambda b, p: adx_di_cross.dmi(b, p, n)[2],
        lambda b, p: holy_grail_pullback.dmi(b, p, n)[2],
        lambda b, p: stochastic_pop.dmi(b, p, n)[2],
        lambda b, p: freqtrade_adx_sma_cross.adx_wilder(b, n, p),
        lambda b, p: hlhb_rsi_ema_adx.adx_wilder(b, p, n),
    )


def _bars(hlc: list[tuple[float, float, float]], step: int = DAY, t0: int = 0) -> Bars:
    t = t0 + np.arange(len(hlc), dtype=np.int64) * step
    h, lo, c = (np.array([r[k] for r in hlc], dtype=float) for k in range(3))
    return Bars(t, c.copy(), h, lo, c, step)


def _real_daily() -> Bars:
    rows = json.loads((FIX / "cycle_regime_btc_1d_2026-10-28.json").read_text())["binance_spot_klines_1d"]
    return Bars.from_rows([[float(x) for x in r[:5]] for r in rows], DAY)


# ------------------------------------------------------------------ Wilder DMI / ADX (five copies in sieve_specs)

# Five daily bars (high, low, close), n = 2, worked by hand:
#   bar 1: up 2, dn -1 -> +DM 2;   TR max(3, 3, 0) = 3
#   bar 2: up -1, dn 2 -> -DM 2;   TR max(4, 0, 4) = 4    p = n: sums TR 7, +DM 2, -DM 2 -> +DI = -DI = 200/7, DX 0
#   bar 3: up 2, dn -1 -> +DM 2;   TR 5   TR 7 - 3.5 + 5 = 8.5, +DM 2 - 1 + 2 = 3, -DM 2 - 1 + 0 = 1
#          +DI 300/8.5, -DI 100/8.5, DX 100 * 2/4 = 50; p = 2n - 1: ADX = mean(0, 50) = 25
#   bar 4: up 1, dn -4 -> +DM 1;   TR 2   TR 6.25, +DM 2.5, -DM 0.5 -> +DI 40, -DI 8, DX 100 * 32/48; ADX (25 + DX) / 2
HAND = [(10, 8, 9), (12, 9, 11), (11, 7, 8), (13, 8, 12), (14, 12, 13)]
HAND_PDI = [math.nan, math.nan, 200 / 7, 300 / 8.5, 40.0]
HAND_MDI = [math.nan, math.nan, 200 / 7, 100 / 8.5, 8.0]
HAND_ADX = [math.nan, math.nan, math.nan, 25.0, (25.0 + 100 * 32 / 48) / 2]


@pytest.mark.parametrize("dmi", DMI, ids=["adx_di_cross", "holy_grail_pullback", "stochastic_pop"])
def test_dmi_matches_a_hand_worked_wilder_example(dmi) -> None:
    b = _bars(HAND)
    pdi, mdi, adx = dmi(b, segment_pos(b.t), 2)
    np.testing.assert_allclose(pdi, HAND_PDI, rtol=1e-12, equal_nan=True)
    np.testing.assert_allclose(mdi, HAND_MDI, rtol=1e-12, equal_nan=True)
    np.testing.assert_allclose(adx, HAND_ADX, rtol=1e-12, equal_nan=True)


def test_adx_wilder_copies_match_the_hand_worked_example() -> None:
    b = _bars(HAND)
    pos = segment_pos(b.t)
    # the two adx_wilder copies take n and pos in opposite order; both give the same ADX as the DMI copies
    np.testing.assert_allclose(freqtrade_adx_sma_cross.adx_wilder(b, 2, pos), HAND_ADX, rtol=1e-12, equal_nan=True)
    np.testing.assert_allclose(hlhb_rsi_ema_adx.adx_wilder(b, pos, 2), HAND_ADX, rtol=1e-12, equal_nan=True)


def test_adx_of_a_steady_trend_is_100_and_mirrors() -> None:
    # every bar 1 higher: +DM 1, -DM 0, TR = max(2, |high - previous close| = 2.5, 0.5) = 2.5 -> +DI = 100/2.5 = 40,
    # -DI 0, DX = ADX = 100
    up = [(101 + i, 99 + i, 99.5 + i) for i in range(40)]
    b = _bars(up)
    pos = segment_pos(b.t)
    n = 14
    for dmi in DMI:
        pdi, mdi, adx = dmi(b, pos, n)
        assert np.isnan(pdi[:n]).all() and np.isnan(adx[: 2 * n - 1]).all()  # first DI at p = n, first ADX at 2n - 1
        np.testing.assert_allclose(pdi[n:], 40.0, rtol=1e-12)
        np.testing.assert_allclose(mdi[n:], 0.0, atol=1e-12)
        np.testing.assert_allclose(adx[2 * n - 1 :], 100.0, rtol=1e-12)
        mp, mm, ma = dmi(b.mirrored(), pos, n)  # a falling market: the DIs swap, ADX stays
        np.testing.assert_allclose(mm[n:], 40.0, rtol=1e-12)
        np.testing.assert_allclose(mp[n:], 0.0, atol=1e-12)
        np.testing.assert_allclose(ma[2 * n - 1 :], 100.0, rtol=1e-12)
    for f in _adx_only(n)[3:]:
        np.testing.assert_allclose(f(b, pos)[2 * n - 1 :], 100.0, rtol=1e-12)


def test_adx_copies_agree_on_real_btc_daily_bars_and_restart_after_a_gap() -> None:
    b = _real_daily()  # 545 Binance spot BTCUSDT days, 01.05.2025 .. 27.10.2026, one segment
    pos = segment_pos(b.t)
    assert len(b) == 545 and int((pos == 0).sum()) == 1
    n = 14
    # the first DI is plain sums of the first n bars (Wilder): check it straight from the raw arrays
    up, dn = np.diff(b.h), -np.diff(b.low)
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum.reduce([b.h[1:] - b.low[1:], np.abs(b.h[1:] - b.c[:-1]), np.abs(b.low[1:] - b.c[:-1])])
    pdi, mdi, adx = adx_di_cross.dmi(b, pos, n)
    assert pdi[n] == pytest.approx(100 * pdm[:n].sum() / tr[:n].sum(), rel=1e-12)
    assert mdi[n] == pytest.approx(100 * mdm[:n].sum() / tr[:n].sum(), rel=1e-12)
    assert np.isnan(adx[: 2 * n - 1]).all() and np.isfinite(adx[2 * n - 1 :]).all()
    assert ((adx[2 * n - 1 :] >= 0) & (adx[2 * n - 1 :] <= 100)).all()
    ref = adx
    for f in _adx_only(n):
        np.testing.assert_allclose(f(b, pos), ref, rtol=1e-9, equal_nan=True)
    for dmi in DMI[1:]:
        p2, m2, _ = dmi(b, pos, n)
        np.testing.assert_allclose(p2, pdi, rtol=1e-9, equal_nan=True)
        np.testing.assert_allclose(m2, mdi, rtol=1e-9, equal_nan=True)
    # a hole of more than the segment gap restarts the count: the second segment equals a fresh run on it
    k = 300
    t = b.t.copy()
    t[k:] += 30 * DAY
    g = Bars(t, b.o, b.h, b.low, b.c, DAY)
    gpos = segment_pos(g.t)
    assert gpos[k] == 0
    tail = g.slice(k, len(g))
    for f in _adx_only(n):
        got = f(g, gpos)
        assert np.isnan(got[k : k + 2 * n - 1]).all()
        np.testing.assert_allclose(got[k:], f(tail, segment_pos(tail.t)), rtol=1e-12, equal_nan=True)
        np.testing.assert_allclose(got[:k], ref[:k], rtol=1e-9, equal_nan=True)  # bars before the hole unchanged


# ------------------------------------------------------------------ kst_signal_cross.sma_valid / tsi_zero_cross.ema_valid


def test_sma_valid_needs_a_fully_finite_window() -> None:
    got = sma_valid(np.array([1, 2, 3, np.nan, 5, 6, 7.0]), 3)
    np.testing.assert_array_equal(got, [np.nan, np.nan, 2.0, np.nan, np.nan, np.nan, 6.0])
    assert np.isnan(sma_valid(np.array([1.0, 2.0]), 3)).all()
    # on real daily closes it is the plain moving mean (the windows hold no NaN)
    c = _real_daily().c
    want = np.convolve(c, np.ones(10) / 10, mode="valid")
    got = sma_valid(c, 10)
    assert np.isnan(got[:9]).all()
    np.testing.assert_allclose(got[9:], want, rtol=1e-12)


def test_ema_valid_seeds_with_the_mean_and_skips_nan() -> None:
    # n = 3, k = 0.5: seed mean(1, 2, 3) = 2, then 0.5 x 4 + 0.5 x 2 = 3, then 0.5 x 5 + 0.5 x 3 = 4
    np.testing.assert_allclose(ema_valid(np.array([1, 2, 3, 4, 5.0]), 3, np.arange(5)), [np.nan, np.nan, 2, 3, 4], equal_nan=True)
    # n = 2, k = 2/3: NaN values are skipped (no output, the state is kept): seed 1.5 at index 2, 2.5, -, then
    # 4 x 2/3 + 2.5 / 3 = 3.5
    got = ema_valid(np.array([np.nan, 1, 2, 3, np.nan, 4.0]), 2, np.arange(6))
    np.testing.assert_allclose(got, [np.nan, np.nan, 1.5, 2.5, np.nan, 3.5], rtol=1e-12, equal_nan=True)
    # a segment start (pos == 0) drops the state and seeds again
    pos = np.array([0, 1, 2, 0, 1, 2])
    got = ema_valid(np.array([1, 2, 3, 10, 20, 30.0]), 2, pos)
    np.testing.assert_allclose(got, [np.nan, 1.5, 1.5 / 3 + 2, np.nan, 15.0, 15.0 / 3 + 20], rtol=1e-12, equal_nan=True)
    # a constant series stays at its value once seeded
    np.testing.assert_allclose(ema_valid(np.full(30, 7.25), 25, np.arange(30))[24:], 7.25, rtol=1e-12)


# ------------------------------------------------------------------ setup_signals.range_setups


FLAT = (101.0, 99.0, 100.0)  # a 30-bar range 99 .. 101, middle 100; TR of every flat bar is 2, so ATR14 = 2


def _wilder_atr(trs: list[float], n: int = 14) -> float:
    a = sum(trs[:n]) / n
    for x in trs[n:]:
        a = (a * (n - 1) + x) / n
    return a


def _range_bars(tail: list[tuple[float, float, float]], n_flat: int = 50) -> Bars:
    return _bars([FLAT] * n_flat + tail, step=H4, t0=1_700_000_000_000 // H4 * H4)


def test_range_setups_s3_sweep_long_and_short_on_the_last_bar() -> None:
    # long: the bar dips to 97 under the range low 99 and closes back in at 99.5 (< middle 100)
    b = _range_bars([(100.5, 97.0, 99.5)])
    a = _wilder_atr([2.0] * 49 + [max(3.5, 0.5, 3.0)])
    [s] = ss.range_setups(b, "4h")
    assert (s.setup, s.side, s.timeframe, s.signal_ts) == ("S3", "long", "4h", int(b.t[-1]))
    assert s.entry == 99.5 and s.target == 100.0  # S3 aims at the range middle
    assert s.stop == pytest.approx(97.0 - 0.5 * a, rel=1e-12)  # 0.5 ATR14 (of the signal bar) under the sweep low
    assert s.indicators["atr14"] == pytest.approx(a, rel=1e-12)
    assert (s.indicators["range_low"], s.indicators["range_high"], s.indicators["extreme"]) == (99.0, 101.0, 97.0)
    assert "S3 (4h)" in s.comment and "свип" in s.comment
    # short: the mirror, a spike to 103 over the range high and a close back in at 100.5 (> middle)
    b = _range_bars([(103.0, 100.5, 100.5)])
    a = _wilder_atr([2.0] * 49 + [max(2.5, 3.0, 0.5)])
    [s] = ss.range_setups(b, "4h")
    assert (s.setup, s.side) == ("S3", "short") and s.entry == 100.5 and s.target == 100.0
    assert s.stop == pytest.approx(103.0 + 0.5 * a, rel=1e-12)


def test_range_setups_s16_deviation_is_decided_on_the_close_back_in() -> None:
    dev = [(99.5, 98.0, 98.5)] * 3  # three closes under the range low 99
    back = (99.8, 98.4, 99.3)  # the close back in, still under the middle
    b = _range_bars([*dev, back])
    trs = [2.0] * 49 + [max(1.5, 0.5, 2.0), 1.5, 1.5, max(1.4, 1.3, 0.1)]
    a = _wilder_atr(trs)
    [s] = ss.range_setups(b, "4h")
    assert (s.setup, s.side, s.signal_ts) == ("S16", "long", int(b.t[-1]))
    assert s.entry == 99.3 and s.target == 101.0  # S16 aims at the opposite edge
    assert s.stop == pytest.approx(98.0 - 0.5 * a, rel=1e-12)  # lowest low of the deviation - 0.5 ATR
    assert "3 закриттів" in s.comment
    # one bar earlier the deviation is still open: nothing on the last bar
    assert ss.range_setups(b.slice(0, len(b) - 1), "4h") == []
    # one flat bar after the signal: the signal is no longer on the last bar
    assert ss.range_setups(_range_bars([*dev, back, FLAT]), "4h") == []


def test_range_setups_needs_range_w_plus_20_bars() -> None:
    assert ss.RANGE_W + 20 == 50
    short = _range_bars([(100.5, 97.0, 99.5)], n_flat=39)  # 40 bars: the detector already sees the sweep
    assert [x.setup for x in ss.range_all(short, "4h", lo=len(short) - 1)] == ["S3"]
    assert ss.range_setups(short, "4h") == []  # but the live scan waits for 50 bars
    assert len(ss.range_setups(_range_bars([(100.5, 97.0, 99.5)], n_flat=49), "4h")) == 1
    assert ss.range_setups(_range_bars([], n_flat=60), "4h") == []  # a flat range alone gives nothing


# ------------------------------------------------------------------ news_events.perp_symbols


def test_perp_symbols_on_real_announcement_titles() -> None:
    # titles as published by Binance / Bybit (the same set tests/test_news_events.py classifies)
    t = "Binance Futures Will Delist USDⓈ-M XEMUSDT, ORBSUSDT and LOOMUSDT Perpetual Contracts (2024-12-09)"
    assert perp_symbols(t) == ["XEMUSDT", "ORBSUSDT", "LOOMUSDT"]
    assert perp_symbols("Delisting of  RONUSDT Perpetual Contract") == ["RONUSDT"]
    assert perp_symbols("Delisting of AI16ZUSDT, AI16ZUSDC Perpetual Contract") == ["AI16ZUSDT"]  # USDC-margined ignored
    assert perp_symbols("Binance Futures Will Delist Multiple USDⓈ-M Perpetual Contracts (2026-10-05)") == []
    assert perp_symbols("Delisting of WIFUSD Inverse Perpetual Contract") == []
    # from a launch body: "2024-12-20 18:30 (UTC): AIXBTUSDT Perpetual Contract"
    assert perp_symbols("2024-12-20 18:30 (UTC): AIXBTUSDT Perpetual Contract") == ["AIXBTUSDT"]
    # multiplier prefixes stay in the symbol (perp_base strips them), order is kept, repeats collapse
    s = perp_symbols("1000PEPEUSDT and BLURUSDT; again 1000PEPEUSDT, then 1INCHUSDT")
    assert s == ["1000PEPEUSDT", "BLURUSDT", "1INCHUSDT"]
    assert [perp_base(x) for x in s] == [("PEPE", 1000), ("BLUR", 1), ("1INCH", 1)]
    # whole words only: lower case, a longer word, a slash pair are not symbols
    assert perp_symbols("btcusdt BTCUSDTM ETH/USDT XYZUSDT2") == []
    # classify_delisting takes its perp tickers from here
    assert classify_delisting("binance", t) == ("perp_delist", perp_symbols(t))


# ------------------------------------------------------------------ premarket_fade.bars_from_rows


def test_bars_from_rows_on_real_binance_1h_klines() -> None:
    rows = json.loads((FIX / "market_flow_btc_1h_2026-10-28.json").read_text())["binance_fapi_klines"]
    assert isinstance(rows[0][4], str)  # Binance sends prices as strings
    bars = bars_from_rows(rows)
    assert list(bars) == [int(r[0]) for r in rows]  # keyed by open time, in row order
    k = len(rows) // 2
    assert bars[int(rows[k][0])] == tuple(float(x) for x in rows[k][1:5])
    assert bars[int(rows[0][0])][3] == 80385.40  # first close, read off the raw row
    # a non-positive close is dropped; a repeated open time keeps the later row
    bad = [[rows[0][0], "1", "1", "1", "0"], [rows[1][0], "1", "1", "1", "-2"]]
    again = [rows[2][0], "1", "2", "0.5", "1.5"]
    got = bars_from_rows([*bad, *rows[2:5], again])
    assert list(got) == [int(r[0]) for r in rows[2:5]] and got[int(rows[2][0])] == (1.0, 2.0, 0.5, 1.5)
    assert bars_from_rows([]) == {}
    # the fade's entry reads these bars: P_pre = close of the bar before t_conv, P_0 = close of the bar at t_conv
    t_conv = int(rows[k][0])
    e = entry_point(bars, t_conv)
    assert e is not None and (e.pre_open, e.entry_open, e.t_e) == (t_conv - HOUR, t_conv, t_conv + HOUR)
    assert (e.p_pre, e.p0) == (float(rows[k - 1][4]), float(rows[k][4]))
    assert e.gap == pytest.approx(math.log(float(rows[k][4]) / float(rows[k - 1][4])), rel=1e-12)


# ------------------------------------------------------------------ ny_midnight_open.ny_days


def test_ny_days_lists_calendar_days_inclusive() -> None:
    assert ny_days(date(2024, 2, 27), date(2024, 3, 1)) == [date(2024, 2, 27), date(2024, 2, 28), date(2024, 2, 29), date(2024, 3, 1)]
    assert ny_days(date(2026, 10, 28), date(2026, 10, 28)) == [date(2026, 10, 28)]
    assert ny_days(date(2026, 10, 28), date(2026, 10, 27)) == []
    days = ny_days(date(2025, 1, 1), date(2025, 12, 31))
    assert len(days) == 365 and all(b - a == timedelta(days=1) for a, b in zip(days, days[1:], strict=False))
    # calendar days, not 24 h steps: across the spring DST switch (08.03.2026) the NY day is 23 h long
    spring = ny_days(date(2026, 3, 7), date(2026, 3, 9))
    assert spring == [date(2026, 3, 7), date(2026, 3, 8), date(2026, 3, 9)]
    assert ny_ms(date(2026, 3, 9), 0) - ny_ms(date(2026, 3, 8), 0) == 23 * HOUR


# ------------------------------------------------------------------ xs_factors.oriented_signal


def test_oriented_signal_flips_the_rank_ic() -> None:
    sig = {"BTC": 0.012, "ETH": -0.004, "SOL": 0.031, "DOGE": 0.0, "XRP": -0.019}
    assert oriented_signal(sig, 1.0) == {"BTC": -0.012, "ETH": 0.004, "SOL": -0.031, "DOGE": 0.0, "XRP": 0.019}
    assert oriented_signal(sig, -1.0) == sig  # sign -1: the low end is already the long side
    assert list(oriented_signal(sig, 1.0)) == list(sig) and oriented_signal({}, 1.0) == {}
    # sign +1 means "long the high-signal symbols": after orienting, the lowest value is the highest raw signal
    o = oriented_signal(sig, 1.0)
    assert min(o, key=o.__getitem__) == max(sig, key=sig.__getitem__) == "SOL"
    ret = [0.05, -0.01, 0.08, 0.00, -0.03]
    raw = rank_ic(list(sig.values()), ret)
    assert raw == pytest.approx(1.0)  # ranks of signal and return coincide here
    assert rank_ic(list(o.values()), ret) == pytest.approx(-raw, abs=1e-12)
