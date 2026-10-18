"""Tests for the book-depth liquidity-exit analytics (IDEA-20261017-market-makers, part A) and the bookDepth parser."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from tradedesk.analytics.market_makers_depth import (
    EventParams,
    depth_day_features,
    detect_events,
    forward_outcomes,
    match_features,
    nearest,
    normalized_depth,
    trailing_mean,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_vision import BinanceVision, parse_book_depth_zip

FIX = Path(__file__).parent / "fixtures" / "binance_vision_BLURUSDT-bookDepth-2026-09-01_head_2026-10-18.zip"


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("X-bookDepth-2026-01-01.csv", text)
    return buf.getvalue()


# ---------------------------------------------------------------- parser + daily features


def test_parse_real_sample() -> None:
    d = parse_book_depth_zip(FIX.read_bytes())
    assert d["ts"].size == 36
    t0 = int(datetime(2026, 9, 1, 0, 0, 4, tzinfo=UTC).timestamp())
    assert d["ts"][0] == t0 and len(set(d["ts"].tolist())) == 3
    assert sorted(set(d["pct"].tolist())) == [-5, -4, -3, -2, -1, -0.2, 0.2, 1, 2, 3, 4, 5]
    assert d["notional"][4] == pytest.approx(51374.88645)  # -1.00 of the first snapshot


def test_features_real_sample_by_hand() -> None:
    d = parse_book_depth_zip(FIX.read_bytes())
    f = depth_day_features(d["ts"], d["pct"], d["notional"])
    assert f["n_snap"] == 3
    snaps = [51374.88645 + 50974.17549, 50770.47653 + 51206.83108, 50852.39791 + 50385.50333]
    assert f["d1"] == pytest.approx(sum(snaps) / 3)
    assert f["d1_med"] == pytest.approx(sorted(snaps)[1])
    assert f["bid1"] == pytest.approx((51374.88645 + 50770.47653 + 50852.39791) / 3)
    assert f["d2"] == pytest.approx((82029.50574 + 77756.12847 + 81425.09582 + 77988.78406 + 81507.01720 + 77167.45631) / 3)


def test_parse_headerless_epoch_ms_and_bad_rows() -> None:
    d = parse_book_depth_zip(_zip("1767225600000,-1,10,100\n1767225600000,1,20,200\n"))
    assert d["ts"].tolist() == [1767225600, 1767225600]
    assert d["notional"].tolist() == [100.0, 200.0]
    with pytest.raises(ProviderError):
        parse_book_depth_zip(_zip("timestamp,percentage,depth,notional\n2026-01-01 00:00:00,-1,10\n"))
    empty = parse_book_depth_zip(_zip("timestamp,percentage,depth,notional\n"))
    assert empty["ts"].size == 0


async def test_provider_book_depth_daily_path_and_404():
    blob = FIX.read_bytes()
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/data/futures/um/daily/bookDepth/BLURUSDT/BLURUSDT-bookDepth-2026-09-01.zip":
            return httpx.Response(200, content=blob)
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    tr = httpx.MockTransport(handler)
    v = BinanceVision(Http("binance_vision", "https://data.binance.vision", transport=tr), Http("s3", "https://s3", transport=tr))
    f = await v.um_book_depth_daily("BLURUSDT", "2026-09-01")
    assert f.data["ts"].size == 36 and f.evidence.provider == "binance_vision"
    with pytest.raises(ProviderError) as e:
        await v.um_book_depth_daily("BLURUSDT", "2026-09-02")
    assert e.value.reason == "data_unavailable"
    n = len(seen)
    with pytest.raises(ProviderError):
        await v.um_book_depth_daily("BLURUSDT", "2026/09/01")
    with pytest.raises(ProviderError):
        await v.um_book_depth_daily("../x", "2026-09-01")
    assert len(seen) == n  # bad arguments never reach the network
    await v.aclose()


def test_features_drop_one_sided_snapshots() -> None:
    ts = np.array([1, 1, 1, 1, 2, 2, 2])  # snapshot 2 lacks the +2 level
    pct = np.array([-1, 1, -2, 2, -1, 1, -2], dtype=float)
    val = np.array([10, 20, 30, 40, 100, 200, 300], dtype=float)
    f = depth_day_features(ts, pct, val)
    assert f["n_snap"] == 1 and f["d1"] == 30 and f["d2"] == 70
    g = depth_day_features(np.array([]), np.array([]), np.array([]))
    assert g["n_snap"] == 0 and np.isnan(g["d1"])


# ---------------------------------------------------------------- normalisation


def test_trailing_mean_and_normalized_depth() -> None:
    x = np.arange(1.0, 11.0)
    m = trailing_mean(x, 3, 2)
    assert np.isnan(m[0]) and m[1] == pytest.approx(1.5) and m[9] == pytest.approx(9.0)
    qv = np.full(40, 100.0)
    nd = normalized_depth(np.full(40, 50.0), qv)
    assert np.isnan(nd[18]) and nd[19] == pytest.approx(0.5) and nd[39] == pytest.approx(0.5)


# ---------------------------------------------------------------- events


def _flat_close(n: int) -> np.ndarray:
    """Close alternating +-1 % log moves: sigma = 0.01-ish, every 2-day move is 0."""
    steps = np.where(np.arange(n) % 2 == 0, 0.01, -0.01)
    steps[0] = 0.0
    return 100 * np.exp(np.cumsum(steps))


def test_exit_event_fires_on_second_low_day() -> None:
    n = 60
    norm = np.ones(n)
    norm[40:42] = 0.4
    assert detect_events(norm, _flat_close(n)) == [41]
    norm[40:42] = 0.55  # above 0.5 x median
    assert detect_events(norm, _flat_close(n)) == []


def test_exit_needs_quiet_price_and_onset() -> None:
    n = 60
    norm = np.ones(n)
    norm[40:42] = 0.4
    c = _flat_close(n)
    c[41:] *= 1.05  # a 5 % move against sigma ~1 % x sqrt(2)
    assert detect_events(norm, c) == []
    assert detect_events(norm, c, EventParams(z_max=10.0)) == [41]
    norm2 = np.ones(n)
    norm2[39:42] = 0.4  # three low days: the event is at 40 (onset), not at 41
    assert detect_events(norm2, _flat_close(n)) == [40]


def test_cooldown_and_entry_direction() -> None:
    n = 90
    norm = np.ones(n)
    norm[40:42] = 0.4
    norm[50:52] = 0.4  # 10 days later: inside the 14-day cooldown
    norm[70:72] = 0.4
    assert detect_events(norm, _flat_close(n)) == [41, 71]
    up = np.ones(n)
    up[40:42] = 2.5
    assert detect_events(up, _flat_close(n), EventParams(ratio=2.0, direction=1)) == [41]
    assert detect_events(up, _flat_close(n)) == []


def test_events_before_t_do_not_depend_on_later_data() -> None:
    rng = np.random.default_rng(1)
    n = 400
    norm = np.exp(rng.normal(0, 0.5, n))
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    base = detect_events(norm, c)
    T = 250
    for seed in range(5):
        r2 = np.random.default_rng(seed + 10)
        n2, c2 = norm.copy(), c.copy()
        n2[T:] = np.exp(r2.normal(0, 0.5, n - T))
        c2[T:] = c[T - 1] * np.exp(np.cumsum(r2.normal(0, 0.03, n - T)))
        assert [t for t in detect_events(n2, c2) if t < T] == [t for t in base if t < T]
    assert any(t < T for t in base)


# ---------------------------------------------------------------- outcomes and matching


def test_forward_outcomes_analytic() -> None:
    c = 100 * np.exp(0.01 * np.arange(20))
    qv = np.full(20, 10.0)
    qv[11:] = 20.0
    o = forward_outcomes(c, qv, 10, (1, 3, 7))
    assert o["r3"] == pytest.approx(0.03) and o["r7"] == pytest.approx(0.07)
    assert o["rv3"] == pytest.approx(0.01)
    assert np.isnan(o["vq3"])  # fewer than 20 days of past volume
    c2 = 100 * np.exp(0.01 * np.arange(60))
    q2 = np.full(60, 10.0)
    q2[41:] = 20.0
    assert forward_outcomes(c2, q2, 40, (3,))["vq3"] == pytest.approx(np.log(2.0))
    assert np.isnan(forward_outcomes(c2, q2, 55, (7,))["r7"])


def test_match_features_and_nearest() -> None:
    n = 60
    c = _flat_close(n)
    qv = np.full(n, 10.0)
    qv[44:46] = 30.0
    f = match_features(c, qv, 45)
    assert f is not None
    z, r7, lv = f
    assert z == pytest.approx(0.0, abs=1e-12)
    assert r7 == pytest.approx(np.log(c[45] / c[38]))
    assert lv == pytest.approx(np.log(3.0))
    assert match_features(c, qv, 10) is None
    cands = np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0], [0.0, 0.2, 0.0], [0.1, 0.01, 0.05]])
    assert nearest((0.0, 0.0, 0.0), cands, 2).tolist() == [0, 3]
    assert nearest((0.0, 0.0, 0.0), np.empty((0, 3)), 2).size == 0
