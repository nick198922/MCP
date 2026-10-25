"""Tests for analytics.gex_flow (IDEA-20261025-gex-taker-flow-range): analytic values and no look-ahead."""

import math
from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.gex_flow import (
    DAY_MS,
    HOUR_MS,
    YEAR_MS,
    FlowBook,
    b76_gamma,
    dealer_gamma,
    expiry_flags,
    h2_day_stat,
    hac_se_indicator,
    interp_flat,
    next_day,
    parse_option,
    permute_within,
    pin_distance,
    rv_log,
    sign_persistence,
    trailing_z,
)
from tradedesk.analytics.options import black76_greeks
from tradedesk.analytics.put_skew import dvol_known_at_entry


def ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


T0 = ms(2024, 3, 5, 9)
EXP = ms(2024, 3, 8, 8)
NAME = "BTC-8MAR24-68000-C"


def tr(name: str, ts: int, side: str, amt: float, iv: float | None = 60.0, idx: float = 65000.0, tid: str = "x") -> dict:
    return {"trade_id": tid, "instrument_name": name, "timestamp": ts, "direction": side, "amount": amt, "iv": iv, "index_price": idx}


def test_parse_option() -> None:
    s = parse_option(NAME)
    assert s is not None and s.expiry_ms == EXP and s.strike == 68000.0 and s.is_call
    p = parse_option("BTC-27DEC24-100000-P")
    assert p is not None and not p.is_call and p.expiry_ms == ms(2024, 12, 27, 8)
    for bad in ("BTC-PERPETUAL", "BTC-8MAR24", "BTC_USDC-8MAR24-68000-C", "BTC-8XXX24-68000-C"):
        assert parse_option(bad) is None


def test_b76_gamma_matches_greeks() -> None:
    g = b76_gamma(65000.0, 68000.0, 0.1, 0.6)
    assert g == pytest.approx(black76_greeks(65000.0, 68000.0, 0.1, 0.6, True)["gamma"], rel=1e-9)
    assert b76_gamma(65000.0, 68000.0, 0.0, 0.6) == 0.0


def test_flow_sign_and_dealer_gamma_value() -> None:
    b = FlowBook()
    b.add([tr(NAME, T0 - 2 * HOUR_MS, "buy", 2.0), tr(NAME, T0 - HOUR_MS, "sell", 0.5, iv=50.0)], T0)
    assert b.series[NAME].q == pytest.approx(1.5) and b.series[NAME].gross == pytest.approx(2.5)
    snap = dealer_gamma(b, T0, iv_max_age_ms=24 * HOUR_MS)
    S = 65000.0
    want = -1.5 * b76_gamma(S, 68000.0, (EXP - T0) / YEAR_MS, 0.5) * S * S / 100.0
    assert snap.gex == pytest.approx(want, rel=1e-12) and snap.gex < 0  # takers net long -> dealers short gamma
    assert snap.gex_put == 0.0 and snap.n_own_iv == 1


def test_block_filter() -> None:
    b = FlowBook(max_amount=100.0)
    b.add([tr(NAME, T0 - HOUR_MS, "buy", 150.0), tr(NAME, T0 - HOUR_MS, "sell", 1.0)], T0)
    assert b.series[NAME].q == -1.0 and b.n_block_skipped == 1 and b.spot == 65000.0


def test_no_lookahead_trades_after_t0() -> None:
    before = [tr(NAME, T0 - HOUR_MS, "buy", 1.0), tr("BTC-8MAR24-60000-P", T0 - 30 * 60_000, "sell", 3.0, iv=70.0)]
    after = [tr(NAME, T0 + 1, "buy", 50.0, iv=200.0, idx=90000.0), tr(NAME, T0 + HOUR_MS, "sell", 7.0)]
    b1, b2 = FlowBook(), FlowBook()
    assert b1.add(before, T0) == []
    later = b2.add(before + after, T0)
    assert later == after
    g1 = dealer_gamma(b1, T0, iv_max_age_ms=24 * HOUR_MS)
    g2 = dealer_gamma(b2, T0, iv_max_age_ms=24 * HOUR_MS)
    assert g1 == g2  # data after t0 changes nothing at t0
    b2.add(later, T0 + HOUR_MS)  # T0 + 1h: G changes only through the prints of that hour
    b3 = FlowBook()
    b3.add(before + after, T0 + HOUR_MS)
    assert dealer_gamma(b2, T0 + HOUR_MS, iv_max_age_ms=24 * HOUR_MS) == dealer_gamma(b3, T0 + HOUR_MS, iv_max_age_ms=24 * HOUR_MS)
    with pytest.raises(ValueError):
        dealer_gamma(b3, T0, iv_max_age_ms=24 * HOUR_MS)


def test_expired_series_out_at_t0() -> None:
    b = FlowBook()
    t0 = ms(2024, 3, 8, 9)  # 1h after the 08:00 expiry of NAME
    b.add([tr(NAME, t0 - 2 * HOUR_MS, "buy", 5.0), tr("BTC-9MAR24-68000-C", t0 - HOUR_MS, "buy", 1.0)], t0)
    snap = dealer_gamma(b, t0, iv_max_age_ms=24 * HOUR_MS)
    assert snap.n_live == 1
    assert b.drop_expired(t0) == 1 and NAME not in b.series


def test_iv_staleness_rule() -> None:
    b = FlowBook()
    old, fresh = T0 - 48 * HOUR_MS, T0 - HOUR_MS
    b.add(
        [
            tr("BTC-8MAR24-64000-C", fresh, "buy", 1.0, iv=50.0),
            tr("BTC-8MAR24-72000-C", fresh, "buy", 1.0, iv=70.0),
            tr("BTC-8MAR24-68000-P", old, "buy", 1.0, iv=99.0),  # stale: interpolated within the expiry
            tr("BTC-29MAR24-68000-P", old, "buy", 1.0, iv=99.0),  # stale, no fresh series in its expiry: excluded
        ],
        T0,
    )
    snap = dealer_gamma(b, T0, iv_max_age_ms=24 * HOUR_MS, profile_expiry_ms=EXP)
    assert (snap.n_own_iv, snap.n_interp_iv, snap.n_excluded) == (2, 1, 1)
    S = 65000.0
    x0, x1, x = math.log(64000 / S), math.log(72000 / S), math.log(68000 / S)
    sig = 0.5 + 0.2 * (x - x0) / (x1 - x0)
    want = -b76_gamma(S, 68000.0, (EXP - T0) / YEAR_MS, sig) * S * S / 100.0
    assert snap.profile[68000.0] == pytest.approx(want, rel=1e-12)
    assert snap.profile_strikes == (64000.0, 68000.0, 72000.0)


def test_interp_flat() -> None:
    c = [(0.0, 1.0), (1.0, 3.0)]
    assert interp_flat(c, -5) == 1.0 and interp_flat(c, 5) == 3.0 and interp_flat(c, 0.25) == pytest.approx(1.5)


def bars_const(t_from: int, n: int, ratio: float, step: float = 1.0) -> dict[int, tuple[float, float, float, float]]:
    out = {}
    c = 100.0
    for i in range(n):
        o = t_from + i * HOUR_MS
        nc = c * math.exp(step * 0.01 * (1 if i % 2 else -1))
        out[o] = (c, max(c, nc) * ratio, min(c, nc) / ratio, nc)
        c = nc
    return out


def test_next_day_parkinson_and_returns() -> None:
    bars = {T0 - HOUR_MS + h * HOUR_MS: (100.0, 101.0, 99.0, 100.0) for h in range(25)}
    r = next_day(bars, T0)
    assert r is not None
    want = 0.5 * math.log(24 * math.log(101 / 99) ** 2 / (4 * math.log(2)))
    assert r["y"] == pytest.approx(want) and r["rets"] == [0.0] * 24
    del bars[T0 + 5 * HOUR_MS]
    assert next_day(bars, T0) is None


def test_rv_uses_only_bars_before_t0() -> None:
    bars = bars_const(T0 - 31 * DAY_MS, 32 * 24, 1.0)
    v = rv_log(bars, T0, 1)
    assert v == pytest.approx(0.5 * math.log(24 * 0.01**2))
    for o in list(bars):
        if o >= T0:
            bars[o] = (1.0, 9.0, 0.5, 7.0)  # change everything from t0 on
    assert rv_log(bars, T0, 1) == pytest.approx(v)
    assert rv_log(bars, T0, 30) is not None


def test_h2_stat() -> None:
    assert h2_day_stat([0.01, -0.01, 0.01], 1e-4) == pytest.approx(-1.0)
    assert h2_day_stat([0.01, 0.01, 0.01], 1e-4) == pytest.approx(1.0)


def test_expiry_flags() -> None:
    assert expiry_flags(date(2024, 3, 28)) == (True, True)  # 29 Mar 2024: last Friday, quarter
    assert expiry_flags(date(2024, 4, 25)) == (True, False)  # 26 Apr 2024
    assert expiry_flags(date(2024, 4, 18)) == (False, False)


def test_dvol_known_at_t0_is_previous_candle() -> None:
    d = ms(2024, 3, 5)
    m = dvol_known_at_entry([(d, 0.5), (d + DAY_MS, 0.7)])
    assert m[d + DAY_MS] == 0.5  # day D at 09:00 sees the candle stamped D-1, never D's own close


def test_trailing_z_no_lookahead() -> None:
    v = [1.0, 2.0, 3.0, 4.0, 100.0]
    z = trailing_z(v, 3, 3)
    assert z[:3] == [None, None, None] and z[3] == pytest.approx((4 - 2) / 1.0)
    assert trailing_z([1.0, 2.0, 3.0, 4.0, -7.0], 3, 3)[:4] == z[:4]


def test_sign_persistence() -> None:
    s = sign_persistence([True] * 5 + [False] * 5)
    assert s["changes"] == 1 and s["share"] == 0.5 and s["n_eff"] < 10
    alt = sign_persistence([True, False] * 50)
    assert alt["changes"] == 99 and alt["rho1"] < -0.9


def test_hac_se_lag0_is_white() -> None:
    rng = np.random.default_rng(1)
    n = 400
    Z = np.column_stack([np.ones(n), rng.normal(size=n)])
    x = (rng.random(n) < 0.3).astype(float)
    e = rng.normal(size=n)
    xt = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
    assert hac_se_indicator(x, Z, e, 0) == pytest.approx(math.sqrt(np.sum(xt**2 * e**2)) / np.sum(xt**2))
    assert hac_se_indicator(x, Z, e, 5) > 0


def test_permute_within_keeps_group_counts() -> None:
    x = [1, 0, 0, 1, 1, 0, 0, 0]
    g = ["a"] * 4 + ["b"] * 4
    p = permute_within(x, g, np.random.default_rng(0))
    assert p[:4].sum() == 2 and p[4:].sum() == 1


def test_pin_distance() -> None:
    prof = {99000.0: 5.0, 101000.0: 9.0, 120000.0: 50.0}
    d = pin_distance(prof, [99000.0, 100000.0, 101000.0, 120000.0], 100000.0, 101500.0)
    assert d == pytest.approx((500 / 1e5, (2500 + 1500 + 500) / 3 / 1e5))
    assert pin_distance({99000.0: -1.0}, [99000.0], 100000.0, 1e5) is None
