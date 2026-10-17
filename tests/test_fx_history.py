"""scripts/fx_history.py: Dukascopy bi5 decoding and aggregation on synthetic records."""

from __future__ import annotations

import asyncio
import lzma
import struct
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fx_history as fx  # noqa: E402
import httpx  # noqa: E402


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


def test_failing_day_does_not_abort_and_is_reported(tmp_path, monkeypatch):
    good = _raw([(0, 112043, 112149, 112040, 112152, 10.0)])

    def handler(req: httpx.Request) -> httpx.Response:
        if "/2026/09/13/" in str(req.url):  # 2026-10-13 (months are 0-based)
            return httpx.Response(503)
        if "/2026/09/11/" in str(req.url):
            return httpx.Response(404)
        return httpx.Response(200, content=good)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(fx.asyncio, "sleep", no_sleep)
    out = tmp_path / "out.csv"

    async def go(allow):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fx.run("EURUSD", date(2026, 10, 11), date(2026, 10, 13), "5m", tmp_path, out, 2, 0.0, allow, c)

    n, missing = asyncio.run(go(False))
    assert (n, missing) == (0, [date(2026, 10, 13)]) and not out.exists()
    assert (tmp_path / "EURUSD" / "2026-10-12.bi5").exists() and (tmp_path / "EURUSD" / "2026-10-11.bi5").read_bytes() == b""
    n, missing = asyncio.run(go(True))
    assert missing == [date(2026, 10, 13)] and n == 1 and out.exists()
