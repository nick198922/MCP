"""Tests for analytics.premarket_inventory (IDEA-20261028-premarket-perp-conversion-fade, step 0)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.analytics.premarket_inventory import (
    DAY_MS,
    HOUR_MS,
    conversion_time,
    days_after,
    hour_confirmed,
    hour_precise,
    mde,
    median_or_none,
    premarket_volumes,
)

FIX = Path(__file__).parent / "fixtures" / "binance_premarket_metusdt_2026-10-28.json"


def ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp() * 1000)


def test_conversion_time_priority_and_gap() -> None:
    c = conversion_time(ms("2025-10-23 13:02"), ms("2025-10-23 00:00"), ms("2025-10-23 14:00"))
    assert c.source == "index_1m" and c.t_ms == ms("2025-10-23 13:02")
    assert c.ann_gap_h == pytest.approx(58 / 60)
    assert conversion_time(None, ms("2025-10-23 00:00"), None).source == "index_1d"
    a = conversion_time(None, None, ms("2025-10-23 14:00"))
    assert a.source == "announcement" and a.ann_gap_h is None
    assert conversion_time(None, None, None) == conversion_time(None, None, None).__class__(None, "none", None)


def test_hour_precise_and_confirmed() -> None:
    t = ms("2025-11-24 11:24")
    far = conversion_time(t, None, ms("2025-11-24 15:00"))  # 3.6 h from the announced time
    near = conversion_time(t, None, ms("2025-11-24 12:00"))
    lone = conversion_time(t, None, None)
    assert not hour_precise(far) and hour_precise(near) and hour_precise(lone)
    assert not hour_precise(conversion_time(None, t, None))  # day precision only
    assert hour_confirmed(near, None)
    assert not hour_confirmed(lone, None)  # no second source
    assert hour_confirmed(lone, t + HOUR_MS) and not hour_confirmed(lone, t + 2 * HOUR_MS)


def test_premarket_volumes_full_days_only() -> None:
    d0 = ms("2026-01-01 00:00")
    daily = [(d0 + i * DAY_MS, float(i + 1)) for i in range(10)]
    conv = d0 + 5 * DAY_MS + 13 * HOUR_MS  # conversion on day 5 at 13:00
    v = premarket_volumes(daily, d0 + 1, conv)  # listing day 0 dropped, conversion day 5 dropped
    assert v == [2.0, 3.0, 4.0, 5.0]
    assert median_or_none(v) == 3.5 and median_or_none([]) is None


def test_premarket_volumes_ignore_data_after_conversion() -> None:
    d0 = ms("2026-01-01 00:00")
    conv = d0 + 6 * DAY_MS
    a = [(d0 + i * DAY_MS, 1.0e6 * (i + 1)) for i in range(12)]
    b = a[:6] + [(t, v * 1000 + 7) for t, v in a[6:]]  # every row from the conversion on changed
    assert premarket_volumes(a, d0 + 1, conv) == premarket_volumes(b, d0 + 1, conv)
    assert premarket_volumes(a[:6], d0 + 1, conv) == premarket_volumes(a, d0 + 1, conv)


def test_days_after_and_mde() -> None:
    conv = ms("2026-03-31 12:30")
    assert days_after(conv, ms("2026-04-07 00:00"), ms("2026-10-28 00:00")) == pytest.approx(7.4791667, rel=1e-6)
    assert days_after(conv, ms("2026-04-07 00:00"), ms("2026-04-05 12:30")) == pytest.approx(5.0)
    assert days_after(conv, None, ms("2026-10-28 00:00")) == 0.0
    assert days_after(conv, ms("2026-03-29 00:00"), ms("2026-10-28 00:00")) == 0.0
    assert mde(0.25, 30) == pytest.approx(0.1278, abs=1e-4)  # card: 12.8 pp
    assert mde(0.25, 60) == pytest.approx(0.0904, abs=1e-4)  # card: 9 pp
    assert math.isinf(mde(0.25, 0))


def test_real_sample_metusdt() -> None:
    """Binance METUSDT, fetched 2026-10-28: index starts 13:02, announced conversion 14:00, spot 3 weeks later."""
    f = json.loads(FIX.read_text())
    c = conversion_time(f["index_1m_first"], f["index_1d_first"], ms(f["ann_convert_utc"]))
    assert c.t_ms == ms("2025-10-23 13:02") and c.ann_gap_h is not None and c.ann_gap_h < 1.0
    assert hour_precise(c) and hour_confirmed(c, f["spot_first"])  # announced time wins over the late spot listing
    daily = [(int(t), float(v)) for t, v in f["perp_1d_open_quotevol"]]
    v = premarket_volumes(daily, daily[0][0] + 1, c.t_ms)
    assert len(v) == 11  # 2025-10-12 .. 2025-10-22: listing day (10-11) and conversion day (10-23) excluded
    assert all(x > 1e6 for x in v)
