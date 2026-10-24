"""Tests for the OI-confirmed liquidation-flush analytics and the archive ``metrics`` parser
(IDEA-20261024-oi-confirmed-liquidation-flush)."""

from __future__ import annotations

import io
import math
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from tradedesk.analytics.oi_flush import (
    OBS_LAG_MS,
    SEMANTICS_SWITCH_MS,
    archive_obs_times,
    basket,
    cluster_bootstrap_diff,
    cluster_se_diff,
    cluster_se_mean,
    effective_cluster_sd,
    mde,
    observation_times,
    oi_at,
    oi_change,
    units_consistent,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_vision import BinanceVision, parse_metrics_zip

FIX = Path(__file__).parent / "fixtures" / "binance_vision_BTCUSDT-metrics-2025-10-10_2100-2135_2026-10-24.zip"
MIN = 60_000


def _ms(h: int, m: int) -> int:
    return int(datetime(2025, 10, 10, h, m, tzinfo=UTC).timestamp() * 1000)


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("X-metrics-2025-10-10.csv", text)
    return buf.getvalue()


# ---------------------------------------------------------------- parser (real sample)


def test_parse_real_sample() -> None:
    rows = parse_metrics_zip(FIX.read_bytes())
    assert len(rows) == 8
    assert rows[0][0] == _ms(21, 0)
    assert rows[0][1] == pytest.approx(91044.269)
    assert rows[0][2] == pytest.approx(10472132120.461727)
    assert [r[0] for r in rows] == [_ms(21, 5 * k) for k in range(8)]


def test_create_time_is_interval_start_on_cascade() -> None:
    """value / OI of the row stamped 21:15 is the price at ~21:20 (1m close 104311.2), not at 21:15 (112137.6)."""
    rows = {int(r[0]): r for r in parse_metrics_zip(FIX.read_bytes())}
    implied = rows[_ms(21, 15)][2] / rows[_ms(21, 15)][1]
    assert abs(implied / 104311.2 - 1) < 0.01
    assert abs(implied / 112137.6 - 1) > 0.05
    assert units_consistent(rows[_ms(21, 15)][1], rows[_ms(21, 15)][2], 104311.2)
    assert not units_consistent(rows[_ms(21, 15)][1], rows[_ms(21, 15)][2], 104.3112)  # x1000 multiplier mismatch


def test_parse_headerless_and_bad_rows() -> None:
    rows = parse_metrics_zip(_zip("2025-10-10 00:05:00,X,2,20,1,1,1,1\n2025-10-10 00:00:00,X,1,10,1,1,1,1\n"))
    assert [r[1] for r in rows] == [1.0, 2.0]  # sorted by time
    with pytest.raises(ProviderError):
        parse_metrics_zip(_zip("create_time,symbol,a,b\n2025-10-10 00:00:00,X,abc,1\n"))


async def test_provider_metrics_daily_path_and_404() -> None:
    blob = FIX.read_bytes()
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2025-10-10.zip":
            return httpx.Response(200, content=blob)
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    f = await v.um_metrics_daily("BTCUSDT", "2025-10-10")
    assert len(f.data) == 8 and f.evidence.provider == "binance_vision"
    with pytest.raises(ProviderError) as e:
        await v.um_metrics_daily("BTCUSDT", "2025-10-11")
    assert e.value.reason == "data_unavailable"
    n = len(seen)
    with pytest.raises(ProviderError):
        await v.um_metrics_daily("../x", "2025-10-10")
    with pytest.raises(ProviderError):
        await v.um_metrics_daily("BTCUSDT", "2025/10/10")
    assert len(seen) == n
    await v.aclose()


# ---------------------------------------------------------------- ΔOI on observation times


def _series() -> tuple[np.ndarray, np.ndarray]:
    rows = parse_metrics_zip(FIX.read_bytes())
    return observation_times([r[0] for r in rows]), np.array([r[1] for r in rows])


def test_oi_change_cascade_bar_by_hand() -> None:
    obs, oi = _series()
    assert obs[0] == _ms(21, 0) + OBS_LAG_MS
    # bar 21:20-21:25: OI at its end (row stamped 21:20) vs OI five minutes before it opened (row stamped 21:10)
    d = oi_change(obs, oi, _ms(21, 25), lookback_ms=10 * MIN, asof=_ms(21, 25))
    assert d == pytest.approx(89433.254 / 90545.522 - 1)
    assert oi_at(obs, oi, _ms(21, 25), asof=_ms(21, 25)) == pytest.approx(89433.254)


def test_oi_change_refuses_lookahead_and_gaps() -> None:
    obs, oi = _series()
    assert oi_change(obs, oi, _ms(21, 25), lookback_ms=10 * MIN, asof=_ms(21, 25) - 1) is None  # not yet observed
    assert oi_change(obs, oi, _ms(21, 2), lookback_ms=10 * MIN, asof=_ms(23, 0)) is None  # off-grid
    assert oi_change(obs, oi, _ms(21, 10), lookback_ms=10 * MIN, asof=_ms(23, 0)) is None  # before the first row


def test_oi_change_ignores_data_after_decision_time() -> None:
    obs, oi = _series()
    t = _ms(21, 25)
    before = oi_change(obs, oi, t, lookback_ms=10 * MIN, asof=t)
    oi2 = oi.copy()
    oi2[obs > t] *= 0.1  # rewrite every snapshot observed after the decision time
    assert oi_change(obs, oi2, t, lookback_ms=10 * MIN, asof=t) == before


def test_archive_obs_times_switch() -> None:
    sw = int(datetime(2024, 3, 4, tzinfo=UTC).timestamp() * 1000)
    assert sw == SEMANTICS_SWITCH_MS
    create = [sw - 10 * MIN, sw - 5 * MIN, sw, sw + 5 * MIN]
    obs = archive_obs_times(create)
    assert obs.tolist() == [sw - 10 * MIN, sw - 5 * MIN, sw + 5 * MIN, sw + 10 * MIN]
    assert np.all(np.diff(obs) > 0)
    # before the switch the snapshot at the bar end T is the row stamped T itself
    old = archive_obs_times([sw - 20 * MIN, sw - 15 * MIN, sw - 10 * MIN])
    assert oi_change(old, np.array([100.0, 90.0, 80.0]), sw - 10 * MIN, lookback_ms=10 * MIN, asof=sw - 10 * MIN) == pytest.approx(-0.2)


def test_basket_boundaries() -> None:
    assert basket(-0.02) == "A"
    assert basket(-0.0201) == "A"
    assert basket(-0.0199) is None
    assert basket(-0.005) is None
    assert basket(-0.0049) == "B"
    assert basket(0.03) == "B"
    assert basket(None) is None
    assert basket(float("nan")) is None


# ---------------------------------------------------------------- cluster SE / MDE


def test_cluster_se_mean_matches_classic_se_for_singletons() -> None:
    x = [1.0, 2.0, 3.0, 4.0]
    s = math.sqrt(sum((v - 2.5) ** 2 for v in x) / 3)
    assert cluster_se_mean(x, ["a", "b", "c", "d"]) == pytest.approx(s / 2)


def test_cluster_se_mean_by_hand() -> None:
    # n = 3, mean 3; cluster sums of (x - m) / n: a = -2/3, b = +2/3; CR1 factor G/(G-1) = 2
    assert cluster_se_mean([1.0, 3.0, 5.0], ["a", "a", "b"]) == pytest.approx(4 / 3)
    assert cluster_se_mean([1.0, 2.0], ["a", "a"]) is None
    assert cluster_se_mean([], []) is None


def test_cluster_se_diff_shared_days_by_hand() -> None:
    # A = [1, 3], B = [2, 6] on days d1, d2: influences sum to +0.5 / -0.5 per day -> SE 1 (independent would be sqrt 5)
    assert cluster_se_diff([1.0, 3.0], ["d1", "d2"], [2.0, 6.0], ["d1", "d2"]) == pytest.approx(1.0)
    assert cluster_se_mean([1.0, 3.0], ["d1", "d2"]) == pytest.approx(1.0)
    assert cluster_se_mean([2.0, 6.0], ["d1", "d2"]) == pytest.approx(2.0)
    assert cluster_se_diff([], [], [1.0], ["d"]) is None


def test_cluster_bootstrap_diff_constant_groups_and_spread() -> None:
    # constant within groups: every resample gives exactly 3 - 1
    b = cluster_bootstrap_diff([3.0, 3.0], ["d1", "d2"], [1.0, 1.0, 1.0], ["d1", "d2", "d3"], n_boot=500)
    assert b.size > 0 and np.allclose(b, 2.0)
    rng = np.random.default_rng(1)
    days = [f"d{i}" for i in range(400)]
    xa, xb = rng.normal(0.01, 0.05, 400).tolist(), rng.normal(0.0, 0.05, 400).tolist()
    bb = cluster_bootstrap_diff(xa, days, xb, days, n_boot=2000)
    se = cluster_se_diff(xa, days, xb, days)
    assert se is not None and float(np.std(bb)) == pytest.approx(se, rel=0.1)
    assert cluster_bootstrap_diff([], [], [1.0], ["d"]).size == 0


def test_mde_and_effective_sd() -> None:
    assert mde(1.0) == pytest.approx(2.8016, abs=1e-4)
    assert mde(None) is None
    assert effective_cluster_sd(0.003, 400) == pytest.approx(0.06)
    assert effective_cluster_sd(None, 10) is None
