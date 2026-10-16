"""scripts/fx_history.py: Dukascopy bi5 decoding and aggregation on synthetic records."""

from __future__ import annotations

import lzma
import struct
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fx_history as fx  # noqa: E402


def _raw(records: list[tuple[int, int, int, int, int, float]]) -> bytes:
    return lzma.compress(b"".join(struct.pack(">IIIIIf", *r) for r in records))


def test_parse_day_scales_points_and_orders_ohlc():
    # (seconds, open, close, low, high, volume) in points; EURUSD has 5 decimals
    raw = _raw([(0, 112043, 112149, 112040, 112152, 10.0), (60, 112149, 112140, 112103, 112171, 5.0)])
    rows = fx.parse_day(raw, date(2026, 10, 12), "EURUSD")
    assert rows[0] == (1791763200000, 1.12043, 1.12152, 1.1204, 1.12149, 10.0)
    assert rows[1][0] == 1791763260000
    assert fx.parse_day(b"", date(2026, 10, 10), "EURUSD") == []


def test_jpy_pairs_use_three_decimals():
    rows = fx.parse_day(_raw([(0, 158321, 157980, 157883, 158948, 1.0)]), date(2026, 10, 14), "USDJPY")
    assert rows[0][1:5] == (158.321, 158.948, 157.883, 157.98)


def test_aggregate_buckets():
    m = 60_000
    rows = [(0, 1.0, 1.2, 0.9, 1.1, 1.0), (m, 1.1, 1.5, 1.0, 1.4, 2.0), (5 * m, 1.4, 1.45, 1.3, 1.35, 3.0)]
    assert fx.aggregate(rows, 5) == [[0, 1.0, 1.5, 0.9, 1.4, 3.0], [5 * m, 1.4, 1.45, 1.3, 1.35, 3.0]]
