"""New-perp listing drift: pure analytics (analytic references) and the archive funding/kline parsers (real samples)."""

from __future__ import annotations

import math
from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.listing_drift import (
    DAY_MS,
    HOUR_MS,
    DailyBars,
    basket_members,
    basket_return,
    bench_return,
    cr1_se,
    design_effect,
    entry_day,
    first_trade_ms,
    funding_gaps,
    funding_received,
    holm,
    icc_anova,
    is_new_token,
    liq_threshold,
    market_age_days,
    mde,
    month_key,
    one_sided_p,
    placebo_day,
    short_leg,
    symbol_eligible,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_vision import BinanceVision, parse_funding_zip, parse_klines_zip, parse_listing

FIX = Path(__file__).parent / "fixtures"
D0 = 19_000  # an arbitrary UTC day index (2022-01-08)


def bars(opens: dict[int, float], highs: dict[int, float] | None = None, closes: dict[int, float] | None = None) -> DailyBars:
    closes = closes or opens
    highs = highs or {d: max(opens[d], closes[d]) for d in opens}
    rows = [[d * DAY_MS, opens[d], highs[d], min(opens[d], closes[d]), closes[d], 10.0, 10.0 * closes[d]] for d in opens]
    return DailyBars.from_rows(rows)


# ---------------------------------------------------------------- universe and timing


def test_symbol_eligible():
    assert symbol_eligible("ARBUSDT", "PERPETUAL", "COIN")
    assert symbol_eligible("FTTUSDT", None, None)  # removed from exchangeInfo: kept as a crypto perp
    assert not symbol_eligible("AAPLUSDT", "TRADIFI_PERPETUAL", "EQUITY")
    assert not symbol_eligible("BTCDOMUSDT", "PERPETUAL", "INDEX")
    assert not symbol_eligible("BTCUSDT_260925", "CURRENT_QUARTER", "COIN")
    assert not symbol_eligible("USDCUSDT", "PERPETUAL", "COIN")
    assert not symbol_eligible("PAXGUSDT", "PERPETUAL", "COIN")
    assert not symbol_eligible("AERGOUSDTSETTLED", None, None)
    assert not symbol_eligible("ETHBUSD", None, None)


def test_first_trade_skips_zero_volume_bars():
    rows = [[3 * HOUR_MS, 1, 1, 1, 1, 5.0], [1 * HOUR_MS, 1, 1, 1, 1, 0.0], [2 * HOUR_MS, 1, 1, 1, 1, 2.0]]
    assert first_trade_ms(rows) == 2 * HOUR_MS
    assert first_trade_ms([[0, 1, 1, 1, 1, 0.0]]) is None


def test_entry_at_least_24h_after_first_trade():
    # listed 12:00 UTC on D0: D0+1 00:00 is only 12 h later, so the entry is D0+2 00:00 (36 h)
    assert entry_day(D0 * DAY_MS + 12 * HOUR_MS) == D0 + 2
    # listed exactly at 00:00: 24 h later is the next midnight
    assert entry_day(D0 * DAY_MS) == D0 + 1
    assert entry_day(D0 * DAY_MS + 1) == D0 + 2
    assert entry_day(D0 * DAY_MS + 12 * HOUR_MS, delay_days=3) == D0 + 5


def test_month_key():
    assert month_key(0) == 1970 * 12
    assert month_key(1_680_000_000_000) == 2023 * 12 + 2  # 2023-03-28


# ---------------------------------------------------------------- the trade


def test_short_leg_analytic_and_truncation():
    b = bars({D0: 100.0, D0 + 1: 110.0, D0 + 2: 90.0}, closes={D0: 105.0, D0 + 1: 95.0, D0 + 2: 80.0})
    leg = short_leg(b, D0, 3)
    assert leg is not None and not leg.liquidated and not leg.truncated
    assert leg.p_entry == 100.0 and leg.p_exit == 80.0 and leg.exit == D0 + 2
    assert leg.pnl == pytest.approx(0.20)  # short gains 20 %
    assert leg.mae == pytest.approx(0.10)  # high 110 on day 1
    # planned exit day missing -> last earlier close, flagged
    leg = short_leg(b, D0, 5)
    assert leg is not None and leg.truncated and leg.exit == D0 + 2
    assert short_leg(b, D0 - 1, 3) is None


def test_short_leg_liquidation_1x_and_3x():
    b = bars(
        {D0: 100.0, D0 + 1: 120.0, D0 + 2: 150.0},
        highs={D0: 101.0, D0 + 1: 198.0, D0 + 2: 250.0},
        closes={D0: 100.0, D0 + 1: 150.0, D0 + 2: 50.0},
    )
    leg = short_leg(b, D0, 3)  # 1x: liquidated when high >= 100 * (1 + 0.975)
    assert leg is not None and leg.liquidated and leg.exit == D0 + 1 and leg.pnl == -1.0
    leg3 = short_leg(b, D0, 3, liq_move=1 / 3, liq_loss=1 / 3)
    assert leg3 is not None and leg3.liquidated and leg3.exit == D0 + 1 and leg3.pnl == pytest.approx(-1 / 3)
    nolq = short_leg(b, D0, 3, liq_move=None)
    assert nolq is not None and nolq.pnl == pytest.approx(0.5) and nolq.mae == pytest.approx(1.5)
    assert liq_threshold(3) == pytest.approx(1 / 3 - 0.025)


def test_trade_does_not_depend_on_bars_after_exit():
    base = {D0 + i: 100.0 + i for i in range(20)}
    a = short_leg(bars(base), D0, 5)
    later = dict(base)
    for d in range(D0 + 5, D0 + 20):
        later[d] = 1000.0  # a squeeze after the exit
    b = short_leg(bars(later), D0, 5)
    assert a == b
    # the entry rule reads no bars at all, only the first-trade time
    assert entry_day(D0 * DAY_MS + 5 * HOUR_MS) == D0 + 2


def test_bench_return():
    b = bars({D0: 50.0, D0 + 1: 55.0}, closes={D0: 52.0, D0 + 1: 60.0})
    assert bench_return(b, D0, D0 + 1) == pytest.approx(0.2)
    assert bench_return(b, D0, D0 + 5) == pytest.approx(0.2)  # last earlier close
    assert bench_return(b, D0 + 3, D0 + 5) is None


# ---------------------------------------------------------------- funding


def test_funding_received_marks_notional_and_respects_window():
    s = D0 * DAY_MS
    recs = [[s - HOUR_MS, 8, 0.5], [s, 8, 0.01], [s + 8 * HOUR_MS, 8, -0.02], [s + DAY_MS, 8, 0.03], [s + 2 * DAY_MS, 8, 9.9]]
    closes = {D0: 100.0, D0 + 1: 200.0}
    # in [s; s + 2 days): 0.01 * 1 - 0.02 * 1 + 0.03 * 2 (notional doubled on day 1)
    assert funding_received(recs, closes, 100.0, s, s + 2 * DAY_MS) == pytest.approx(0.05)


def test_funding_gaps():
    s = D0 * DAY_MS
    regular = [[s + k * 8 * HOUR_MS, 8, 0.0001] for k in range(9)]  # 3 days of 8h settlements
    assert funding_gaps(regular, s, s + 3 * DAY_MS) == []
    holed = [r for i, r in enumerate(regular) if i not in (3, 4)]
    assert funding_gaps(holed, s, s + 3 * DAY_MS) == [(s + 16 * HOUR_MS, s + 40 * HOUR_MS)]
    # interval switches to 4h (typical after a new listing hits the cap): no false gap
    mixed = [[s, 8, 0.0], [s + 8 * HOUR_MS, 4, 0.0], [s + 12 * HOUR_MS, 4, 0.0], [s + 16 * HOUR_MS, 4, 0.0], [s + 20 * HOUR_MS, 4, 0.0]]
    assert funding_gaps(mixed, s, s + 24 * HOUR_MS) == []
    assert funding_gaps([], s, s + DAY_MS) == [(s, s + DAY_MS)]
    # a series that ends early leaves a hole up to the window end
    assert funding_gaps(regular[:3], s, s + 3 * DAY_MS) == [(s + 16 * HOUR_MS, s + 3 * DAY_MS)]


# ---------------------------------------------------------------- placebo and basket


def test_placebo_day_range_and_determinism():
    days = {placebo_day(f"SYM{i}USDT", D0) for i in range(300)}
    assert min(days) >= D0 + 120 and max(days) <= D0 + 240
    assert len(days) > 50
    assert placebo_day("ARBUSDT", D0) == placebo_day("ARBUSDT", D0)


def test_basket_members_point_in_time():
    entry = D0 + 400
    first = {"OLD1": D0, "OLD2": D0, "OLD3": D0, "YOUNG": D0 + 100, "THIN": D0}
    qv = {
        "OLD1": {d: 10.0 for d in range(entry - 30, entry + 30)},
        "OLD2": {d: 20.0 for d in range(entry - 30, entry)},
        "OLD3": {d: 10.0 for d in range(entry - 30, entry)},
        "YOUNG": {d: 999.0 for d in range(entry - 30, entry)},
        "THIN": {d: 999.0 for d in range(entry - 10, entry)},
    }
    m = basket_members(entry, first, qv, top_n=2)
    assert m == ["OLD2", "OLD1"]  # OLD1 / OLD3 tie broken by name; YOUNG too young; THIN < 20 days
    # changing data on or after the entry day does not change the selection
    qv2 = {k: dict(v) for k, v in qv.items()}
    for d in range(entry, entry + 30):
        qv2["OLD3"][d] = 1e9
    assert basket_members(entry, first, qv2, top_n=2) == m
    b = {"A": bars({D0: 10.0}, closes={D0: 11.0}), "B": bars({D0: 10.0}, closes={D0: 9.0}), "C": bars({D0 + 1: 1.0})}
    assert basket_return(["A", "B", "C"], b, D0, D0) == pytest.approx(0.0)


# ---------------------------------------------------------------- statistics


def test_icc_design_effect_and_mde():
    # identical values within clusters, different between -> ICC 1
    assert icc_anova([1, 1, 5, 5, 9, 9], [0, 0, 1, 1, 2, 2]) == pytest.approx(1.0)
    # equal cluster means -> no between-cluster variance -> ICC <= 0
    assert icc_anova([0, 2, 0, 2, 0, 2], [0, 0, 1, 1, 2, 2]) < 0
    # equal sizes m: deff = 1 + (m - 1) * ICC; negative ICC is floored at 0
    assert design_effect([0] * 5 + [1] * 5, 0.1) == pytest.approx(1.4)
    assert design_effect([0] * 5 + [1] * 5, -0.3) == pytest.approx(1.0)
    assert mde(0.40, 400) == pytest.approx(2.8 * 0.40 / 20)
    assert mde(0.40, 400, 1.44) == pytest.approx(2.8 * 0.40 * 1.2 / 20)


def test_cr1_se_matches_classical_with_singleton_clusters():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert cr1_se(xs, [0, 1, 2, 3]) == pytest.approx(math.sqrt(5 / 3) / 2)  # = sd / sqrt(n)
    # perfectly clustered pairs: SE grows
    assert cr1_se([1, 1, 3, 3], [0, 0, 1, 1]) > cr1_se([1, 1, 3, 3], [0, 1, 2, 3])


def test_holm_and_one_sided_p():
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert one_sided_p(1.959964) == pytest.approx(0.025, abs=1e-6)


def test_market_age_and_new_token():
    t0 = D0 * DAY_MS
    assert market_age_days(t0, [None, t0 - 45 * DAY_MS, t0 + DAY_MS]) == pytest.approx(45)
    assert market_age_days(t0, [None]) is None
    assert is_new_token(t0, [t0 - 30 * DAY_MS])
    assert not is_new_token(t0, [t0 - 31 * DAY_MS])
    assert is_new_token(t0, [])  # no evidence anywhere: first market is this perp


# ---------------------------------------------------------------- archive parsers (real samples, 2026-10-15)


def test_parse_real_archive_samples():
    # data.binance.vision, fetched 2026-10-15: ARBUSDT perp 1h, March 2023 (listing month)
    rows = parse_klines_zip((FIX / "binance_vision_ARBUSDT-1h-2023-03_2026-10-15.zip").read_bytes())
    assert len(rows) == 201
    assert rows[0][0] == 1_679_583_600_000  # 2023-03-23 15:00 UTC: first traded hour
    assert rows[0][1] == pytest.approx(1.3525)
    assert first_trade_ms(rows) == 1_679_583_600_000
    # funding archive: [calc_time, interval_hours, rate]; one settlement is stamped before the first trade
    f = parse_funding_zip((FIX / "binance_vision_ARBUSDT-fundingRate-2023-03_2026-10-15.zip").read_bytes())
    assert len(f) == 26 and f[0][1] == 8.0 and f[0][2] == pytest.approx(0.0001)
    assert f[1][2] == pytest.approx(0.00068996)
    assert funding_gaps(f, 1_679_616_000_000, 1_680_307_200_000) == []  # 2023-03-24 .. 2023-04-01: complete
    # FTT in the November 2022 collapse: funding hit -2.5 % per 8h (short pays)
    f = parse_funding_zip((FIX / "binance_vision_FTTUSDT-fundingRate-2022-11_2026-10-15.zip").read_bytes())
    assert len(f) == 126 and min(r[2] for r in f) == pytest.approx(-0.025)
    dirs, keys, nxt = parse_listing((FIX / "binance_vision_spot_listing_ARBUSDT_1d_2026-10-15.xml").read_bytes())
    assert nxt is None and keys[0].endswith("ARBUSDT-1d-2023-03.zip")


async def test_provider_funding_monthly_and_404():
    blob = (FIX / "binance_vision_ARBUSDT-fundingRate-2023-03_2026-10-15.zip").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/data/futures/um/monthly/fundingRate/ARBUSDT/ARBUSDT-fundingRate-2023-03.zip":
            return httpx.Response(200, content=blob)
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    got = await v.um_funding_monthly("ARBUSDT", 2023, 3)
    assert len(got.data) == 26 and got.evidence.provider == "binance_vision"
    with pytest.raises(ProviderError) as e:
        await v.um_funding_monthly("ARBUSDT", 2023, 2)
    assert e.value.reason == "data_unavailable"
    with pytest.raises(ProviderError):
        await v.um_funding_monthly("../x", 2023, 3)
    with pytest.raises(ProviderError):
        parse_funding_zip(b"not a zip")
    await v.aclose()
