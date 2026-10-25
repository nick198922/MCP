"""Mark-price stop trigger (IDEA-20261025-mark-price-stop-trigger): analytic expectations on hand-made bars."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars
from tradedesk.analytics.mark_stop import (
    align,
    divergent,
    mde_clustered,
    shifted_mark,
    simulate_mark_stop,
)
from tradedesk.analytics.stop_management import Fees, StopRule, risk_unit, simulate_stop_rule

A = StopRule("A")
B = StopRule("B", be_frac=0.5)
NOFEE = Fees(0.0, 0.0, 0.0, 0.0)
HOLD = 100 * HOUR_MS


def bars(rows: list[tuple[float, float, float, float]], t0: int = 0) -> Bars:
    return Bars.from_rows([[t0 + i * HOUR_MS, *r] for i, r in enumerate(rows)], HOUR_MS)


def test_wick_on_last_only_continues_to_target():
    # entry 100, stop 95, target 110; bar 1 wicks to 94 on last, mark low 97: divergent, then target on bar 2
    last = bars([(100, 101, 99, 100), (100, 101, 94, 100), (100, 111, 99, 110)])
    mark = bars([(100, 101, 99.5, 100), (100, 100.5, 97, 100), (100, 110.5, 99.5, 110)])
    lo = simulate_stop_rule(last, 0, 100, 95, 110, HOLD, A, fees=NOFEE)
    mo = simulate_mark_stop(last, align(last, mark), 0, 100, 95, 110, HOLD, A, fees=NOFEE)
    assert lo.exit_kind == "stop" and lo.r_net == pytest.approx(-1.0)
    assert mo.exit_kind == "target" and mo.r_net == pytest.approx(2.0)
    assert divergent(lo, last, align(last, mark), 100, 95, NOFEE) is True


def test_same_bar_touch_gives_the_same_fill_and_fees():
    last = bars([(100, 101, 94, 96)])
    mark = bars([(100, 101, 94.5, 96)])
    lo = simulate_stop_rule(last, 0, 100, 95, 110, HOLD, A)
    mo = simulate_mark_stop(last, align(last, mark), 0, 100, 95, 110, HOLD, A)
    assert mo.exit_kind == lo.exit_kind == "stop"
    assert mo.r_net == pytest.approx(lo.r_net)
    assert divergent(lo, last, align(last, mark), 100, 95) is False


def test_late_mark_stop_pays_the_close_and_never_beats_the_stop():
    # last wicks below at bar 1 (mark holds), at bar 2 last opens at 94 and closes at 90, mark touches 95 inside the bar
    last = bars([(100, 101, 99, 100), (100, 100, 94, 99), (94, 95, 89, 90)])
    mark = bars([(100, 101, 99, 100), (100, 100, 96, 99), (96, 96, 94, 92)])
    mk = align(last, mark)
    mo = simulate_mark_stop(last, mk, 0, 100, 95, 110, HOLD, A, fees=NOFEE)
    assert mo.exit_kind == "stop" and mo.exit == pytest.approx(90.0)  # min(stop, last.o, last.c)
    assert mo.r_net == pytest.approx(-2.0)
    assert simulate_mark_stop(last, mk, 0, 100, 95, 110, HOLD, A, fees=NOFEE, fill="stop").exit == pytest.approx(94.0)
    assert simulate_mark_stop(last, mk, 0, 100, 95, 110, HOLD, A, fees=NOFEE, fill="worst").exit == pytest.approx(89.0)


def test_mark_gap_fills_at_last_open_capped_by_stop():
    last = bars([(100, 101, 99, 100), (96, 97, 93, 94)])
    mark = bars([(100, 101, 99, 100), (94.5, 95, 94, 94.5)])
    mo = simulate_mark_stop(last, align(last, mark), 0, 100, 95, 110, HOLD, A, fees=NOFEE)
    assert mo.exit_kind == "stop_gap" and mo.exit == pytest.approx(95.0)  # min(stop 95, last.o 96)


def test_mark_earlier_than_last():
    last = bars([(100, 101, 96, 100), (100, 111, 99, 110)])
    mark = bars([(100, 101, 94.9, 100), (100, 111, 99, 110)])
    lo = simulate_stop_rule(last, 0, 100, 95, 110, HOLD, A, fees=NOFEE)
    mo = simulate_mark_stop(last, align(last, mark), 0, 100, 95, 110, HOLD, A, fees=NOFEE)
    assert lo.exit_kind == "target"
    assert mo.exit_kind == "stop" and mo.exit == pytest.approx(95.0)


def test_break_even_moves_on_last_and_fires_on_mark():
    # target 110, trigger 105 on last at bar 0; BE from bar 1; bar 1: last low 99 (BE hit on last), mark low 100.5
    last = bars([(100, 106, 99.5, 104), (104, 104, 99, 103), (103, 111, 102, 110)])
    mark = bars([(100, 105.5, 99.6, 104), (104, 104, 100.5, 103), (103, 110.5, 102, 110)])
    mk = align(last, mark)
    lo = simulate_stop_rule(last, 0, 100, 95, 110, HOLD, B, fees=NOFEE)
    mo = simulate_mark_stop(last, mk, 0, 100, 95, 110, HOLD, B, fees=NOFEE)
    assert lo.exit_kind == "be" and lo.r_net == pytest.approx(0.0)
    assert mo.exit_kind == "target" and mo.r_net == pytest.approx(2.0)
    assert divergent(lo, last, mk, 100, 95, NOFEE) is True


def test_mark_identical_to_last_reproduces_last_branch():
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    o = np.r_[100, c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, 400))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, 400))
    last = bars(list(zip(o, h, lo, c, strict=True)))
    mk = align(last, last)
    for t in range(0, 300, 7):
        e = float(last.c[t])
        a = simulate_stop_rule(last, (t + 1) * HOUR_MS, e, e * 0.95, e * 1.08, HOLD, B)
        b = simulate_mark_stop(last, mk, (t + 1) * HOUR_MS, e, e * 0.95, e * 1.08, HOLD, B)
        assert a.exit_kind == b.exit_kind and a.exit_ms == b.exit_ms and b.r_net == pytest.approx(a.r_net)


def test_short_mirrored():
    # short at 100, stop 105, target 90; last spikes to 106, mark only to 103, then falls to target
    last = bars([(100, 101, 99, 100), (100, 106, 99, 100), (100, 101, 89, 90)])
    mark = bars([(100, 101, 99, 100), (100, 103, 99, 100), (100, 101, 89.5, 90)])
    lm, mm = last.mirrored(), mark.mirrored()
    mo = simulate_mark_stop(lm, align(lm, mm), 0, -100, -105, -90, HOLD, A, fees=NOFEE)
    assert mo.exit_kind == "target" and mo.r_net == pytest.approx(2.0)


def test_missing_mark_bar_drops_the_trade():
    last = bars([(100, 101, 99, 100), (100, 101, 99, 100)])
    mark = bars([(100, 101, 99, 100)])
    assert simulate_mark_stop(last, align(last, mark), 0, 100, 95, 110, HOLD, A) is None


def test_result_before_t_does_not_depend_on_bars_after_t():
    last = bars([(100, 101, 99, 100), (100, 101, 94, 100), (100, 101, 94.5, 100), (100, 111, 99, 110), (110, 120, 50, 60)])
    mark = bars([(100, 101, 99, 100), (100, 101, 96, 100), (100, 101, 94.8, 100), (100, 111, 99, 110), (110, 120, 50, 60)])
    base = simulate_mark_stop(last, align(last, mark), 0, 100, 95, 110, HOLD, A)
    assert base.exit_kind == "stop" and base.exit_ms == 3 * HOUR_MS
    l2 = Bars(last.t, last.o.copy(), last.h.copy(), last.low.copy(), last.c.copy(), HOUR_MS)
    m2 = Bars(mark.t, mark.o.copy(), mark.h.copy(), mark.low.copy(), mark.c.copy(), HOUR_MS)
    for x in (l2, m2):
        x.o[3:], x.h[3:], x.low[3:], x.c[3:] = 1.0, 1.0, 1.0, 1.0
    alt = simulate_mark_stop(l2, align(l2, m2), 0, 100, 95, 110, HOLD, A)
    assert alt.exit_ms == base.exit_ms and alt.r_net == pytest.approx(base.r_net)


def test_placebo_shift_uses_ratios_from_another_bar():
    last = bars([(100, 102, 98, 101)] * 4)
    mark = bars([(100, 101, 99, 100.5), (100, 101, 99, 100.5), (100, 102, 98, 101), (100, 102, 98, 101)])
    p = shifted_mark(last, mark, 2 * HOUR_MS)
    assert len(p) == 2  # bars 2, 3 have no source 2h later
    assert p.low[0] == pytest.approx(98 * 98 / 98) and p.h[0] == pytest.approx(102.0)
    p0 = shifted_mark(last, mark, 0)
    assert p0.low[0] == pytest.approx(99.0) and p0.h[0] == pytest.approx(101.0)
    assert np.all(p.low <= np.minimum(p.o, p.c)) and np.all(p.h >= np.maximum(p.o, p.c))


def test_mde_clustered_matches_iid_formula_with_singleton_clusters():
    rng = np.random.default_rng(3)
    f = rng.random(20_000) < 0.02
    mde = mde_clustered(f, np.arange(f.size), mu=1.0, sd=1.2, z=2.8)
    q = f.mean()
    want = 2.8 * np.sqrt((q * (1.2**2 + 1.0**2) - (q * 1.0) ** 2) / f.size)
    assert mde == pytest.approx(want, rel=1e-6)


async def test_provider_mark_price_monthly_path_and_404():
    from pathlib import Path

    import httpx

    from tradedesk.core.errors import ProviderError
    from tradedesk.core.http import Http
    from tradedesk.providers.binance_vision import BinanceVision

    # kline-format archive sample of data.binance.vision (2026-10-15); markPriceKlines share the kline CSV layout
    blob = (Path(__file__).parent / "fixtures" / "binance_vision_ARBUSDT-1h-2024-03_pi1h_m_2026-10-15.zip").read_bytes()
    route = "/data/futures/um/monthly/markPriceKlines/ARBUSDT/1h/ARBUSDT-1h-2024-03.zip"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == route:
            return httpx.Response(200, content=blob)
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    got = await v.um_mark_price_monthly("ARBUSDT", "1h", 2024, 3)
    assert len(got.data) == 744 and got.evidence.provider == "binance_vision"
    with pytest.raises(ProviderError) as e:
        await v.um_mark_price_monthly("ARBUSDT", "1h", 2024, 4)
    assert e.value.reason == "data_unavailable"


def test_risk_unit_and_fees_identical_across_branches():
    last = bars([(100, 101, 99, 100)] * 3)
    lo = simulate_stop_rule(last, 0, 100, 95, 110, 3 * HOUR_MS, A)
    mo = simulate_mark_stop(last, align(last, last), 0, 100, 95, 110, 3 * HOUR_MS, A)
    assert lo.exit_kind == mo.exit_kind == "time"
    assert mo.cost_r == pytest.approx(lo.cost_r)
    assert risk_unit(100, 95) > 5
