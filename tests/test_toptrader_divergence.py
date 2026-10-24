"""Top-trader vs crowd divergence (IDEA-20261024-toptrader-vs-crowd-divergence): archive ratio parser on a real
sample, snapshot timing across the archive semantics switch, causal z-scores, IC, placebo and the power gate."""

from __future__ import annotations

import io
import math
import statistics
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from tradedesk.analytics.oi_flush import SEMANTICS_SWITCH_MS
from tradedesk.analytics.toptrader_divergence import (
    DAY_MS,
    day_snapshot,
    divergence,
    gate,
    hit_rate,
    placebo_within_asset,
    trailing_z,
    weekly_ic,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_vision import BinanceVision, parse_metrics_ratios_zip

FIX = Path(__file__).parent / "fixtures" / "binance_vision_BTCUSDT-metrics-2025-10-10_2100-2135_2026-10-24.zip"
DAY_2025_10_10 = int(datetime(2025, 10, 10, tzinfo=UTC).timestamp() * 1000) // DAY_MS


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("X-metrics.csv", text)
    return buf.getvalue()


def test_parse_ratios_real_sample() -> None:
    rows = parse_metrics_ratios_zip(FIX.read_bytes())
    assert len(rows) == 8 and len(rows[0]) == 7
    r = rows[0]  # 2025-10-10 21:00:00 row of the archive file
    assert r[0] == int(datetime(2025, 10, 10, 21, 0, tzinfo=UTC).timestamp() * 1000)
    assert r[1] == pytest.approx(91044.269)
    assert (r[3], r[4], r[5]) == pytest.approx((1.53654118, 1.614415, 1.25636907))
    assert r[6] == pytest.approx(1.044085)


def test_parse_ratios_header_order_empty_cells_and_errors() -> None:
    text = (
        "create_time,symbol,count_long_short_ratio,sum_open_interest,sum_open_interest_value,"
        "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        "2025-10-10 00:05:00,X,2.0,1,10,3,4,\n2025-10-10 00:00:00,X,1.5,1,10,3,4,1\n"
    )
    rows = parse_metrics_ratios_zip(_zip(text))
    assert [r[5] for r in rows] == [1.5, 2.0]  # crowd column found by name, rows sorted
    assert math.isnan(rows[1][6])
    headerless = parse_metrics_ratios_zip(_zip("2025-10-10 00:00:00,X,1,10,3,4,1.5,1\n"))
    assert headerless[0][1:] == [1.0, 10.0, 3.0, 4.0, 1.5, 1.0]
    with pytest.raises(ProviderError):
        parse_metrics_ratios_zip(_zip("2025-10-10 00:00:00,X,1,10,abc,4,1.5,1\n"))


async def test_provider_ratios_daily_path_and_404() -> None:
    blob = FIX.read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2025-10-10.zip":
            return httpx.Response(200, content=blob)
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    f = await v.um_metrics_ratios_daily("BTCUSDT", "2025-10-10")
    assert len(f.data) == 8 and f.data[0][5] == pytest.approx(1.25636907)
    with pytest.raises(ProviderError) as e:
        await v.um_metrics_ratios_daily("BTCUSDT", "2025-10-11")
    assert e.value.reason == "data_unavailable"
    with pytest.raises(ProviderError):
        await v.um_metrics_ratios_daily("../x", "2025-10-10")


# ---------------------------------------------------------------- snapshot timing


def _row(ms: int, top_pos: float = 1.5, crowd: float = 1.2) -> list[float]:
    return [float(ms), 1.0, 1.0, 1.1, top_pos, crowd, 1.0]


def test_snapshot_real_sample_and_cutoff() -> None:
    rows = parse_metrics_ratios_zip(FIX.read_bytes())
    s = day_snapshot(rows, DAY_2025_10_10)
    assert s is not None  # after the switch: the 21:35 row is observed at 21:40
    assert s["obs_ms"] == int(datetime(2025, 10, 10, 21, 40, tzinfo=UTC).timestamp() * 1000)
    assert day_snapshot(rows, DAY_2025_10_10 - 1) is None  # rows of another day never count


def test_snapshot_respects_semantics_switch() -> None:
    old_day = SEMANTICS_SWITCH_MS // DAY_MS - 10
    new_day = SEMANTICS_SWITCH_MS // DAY_MS + 10
    for day, want in ((old_day, 23 * 60 + 55), (new_day, 23 * 60 + 50)):
        base = day * DAY_MS
        rows = [_row(base + m * 60_000, top_pos=float(m)) for m in range(0, 24 * 60, 5)]
        s = day_snapshot(rows, day)
        assert s is not None
        assert s["top_pos"] == float(want)  # old: 23:55 observed at 23:55; new: 23:55 observed 00:00 -> 23:50 row
        assert s["obs_ms"] <= (day + 1) * DAY_MS - 5 * 60_000


def test_snapshot_skips_bad_ratios() -> None:
    day = SEMANTICS_SWITCH_MS // DAY_MS - 10
    base = day * DAY_MS
    rows = [_row(base + 60 * 60_000, top_pos=2.0), _row(base + 120 * 60_000, crowd=float("nan")), _row(base + 180 * 60_000, crowd=0.0)]
    s = day_snapshot(rows, day)
    assert s is not None and s["top_pos"] == 2.0
    assert day_snapshot([], day) is None


# ---------------------------------------------------------------- z-scores and D


def test_trailing_z_analytic() -> None:
    z = trailing_z([1.0, 2.0, 3.0], window=3, min_obs=3)
    assert z[:2] == [None, None]
    assert z[2] == pytest.approx((3 - 2) / 1.0)  # mean 2, sample SD 1
    assert trailing_z([5.0, 5.0, 5.0], window=3, min_obs=2)[2] is None  # SD 0
    assert trailing_z([1.0, None, 3.0, 4.0], window=3, min_obs=2)[3] == pytest.approx((4 - 3.5) / statistics.stdev([3.0, 4.0]))


def test_divergence_signs_and_causality() -> None:
    rng = np.random.default_rng(1)
    top = list(np.exp(rng.normal(0, 0.1, 40)))
    crowd = list(np.exp(rng.normal(0, 0.1, 40)))
    d = divergence(top, crowd, window=13, min_obs=10)
    assert all(v is None for v in d[:9]) and d[9] is not None
    # results before t do not change when data after t changes
    t = 25
    top2 = top[:t] + [x * 5 for x in top[t:]]
    crowd2 = crowd[:t] + [None] * (40 - t)
    d2 = divergence(top2, crowd2, window=13, min_obs=10)
    assert d2[:t] == d[:t]
    # same ratio in both groups -> D == 0; the top group alone rising -> D > 0
    assert all(v == pytest.approx(0.0) for v in divergence(top, top)[9:])
    up = top[:-1] + [top[-1] * 3]
    assert divergence(up, top)[-1] > 0
    with pytest.raises(ValueError):
        divergence([1.0], [1.0, 2.0])


# ---------------------------------------------------------------- IC, placebo, hit rate, gate


def test_weekly_ic_and_hit_rate() -> None:
    assert weekly_ic([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert weekly_ic([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert math.isnan(weekly_ic([1, 2], [1, 2]))
    assert hit_rate([1, -1, 1, 0], [0.1, 0.2, -0.3, 0.5]) == (pytest.approx(1 / 3), 3)


def test_placebo_destroys_timing_but_not_cross_section() -> None:
    rng = np.random.default_rng(3)
    names = [f"A{i}" for i in range(20)]
    timed = []
    for _ in range(60):
        s = rng.normal(size=20)
        timed.append({n: (float(s[i]), float(s[i] + rng.normal(0, 0.5))) for i, n in enumerate(names)})
    real = statistics.fmean(weekly_ic([v[0] for v in w.values()], [v[1] for v in w.values()]) for w in timed)
    null = placebo_within_asset(timed, 50, seed=1)
    assert real > 0.8 and abs(float(np.mean(null))) < 0.05  # timing signal vanishes under the shuffle
    # a persistent per-asset signal (same value every week) survives the within-asset shuffle unchanged
    fixed = rng.normal(size=20)
    persist = [{n: (float(fixed[i]), float(fixed[i] + rng.normal(0, 0.5))) for i, n in enumerate(names)} for _ in range(30)]
    p2 = placebo_within_asset(persist, 5, seed=2)
    obs = statistics.fmean(weekly_ic([v[0] for v in w.values()], [v[1] for v in w.values()]) for w in persist)
    assert np.allclose(p2, obs)


def test_gate() -> None:
    ics = [0.1, -0.1] * 100  # SD ~0.1003, n 200 -> SE ~0.0071, MDE ~0.0199
    g = gate(15, ics, 0.005)
    assert g["pass"] and g["mde"] == pytest.approx(2.8 * statistics.stdev(ics) / math.sqrt(200))
    assert not gate(11, ics, 0.005)["pass"]
    assert not gate(15, ics, 0.01)["pass"]  # NW SE larger -> MDE 0.028
