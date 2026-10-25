"""Binance public data archive (`data.binance.vision`): historical klines as zipped CSV, no key.

Unlike the live API (ccxt), the archive keeps delisted symbols, which research needs to avoid survivorship bias.
Two hosts: files on `data.binance.vision`, directory listings on the S3 bucket endpoint (XML, paged by `marker`).
Used by research scripts, not by MCP tools. All I/O goes through `core.http.Http`.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from typing import Any

import numpy as np

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

FILES_URL = "https://data.binance.vision"
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com"
BUCKET = "data.binance.vision"
UM_KLINES = "data/futures/um"
SPOT = "data/spot"
VALID_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d")
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
# Archive symbols are mostly [A-Z0-9] but some bases are not Latin (e.g. a CJK meme coin); forbid only path syntax.
_SYMBOL = re.compile(r"^[^/\\.?&#%\s]+$")


def _csv_text(blob: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            raise ProviderError("binance_vision", "backend_error", "zip without csv")
        return zf.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile as e:
        raise ProviderError("binance_vision", "backend_error", f"bad zip: {e}") from None


def parse_klines_zip(blob: bytes) -> list[list[float]]:
    """Rows ``[open_ms, open, high, low, close, base_volume, quote_volume]`` from an archive kline zip.

    Handles files with and without a header row and microsecond timestamps (newer archives)."""
    text = _csv_text(blob)
    out: list[list[float]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 8 or not row[0].strip().isdigit():
            continue  # header or junk
        ts = int(row[0])
        if ts > 10**14:  # microseconds
            ts //= 1000
        out.append([float(ts), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7])])
    out.sort(key=lambda r: r[0])
    return out


def parse_klines_taker_zip(blob: bytes) -> list[list[float]]:
    """Rows ``[open_ms, open, high, low, close, base_volume, quote_volume, taker_buy_base, taker_buy_quote]``.

    Same archive file as ``parse_klines_zip`` (which keeps its 7-column layout for existing callers) plus the
    aggressor columns 9 and 10: base / quote volume of trades where the taker bought. Header optional; microsecond
    timestamps (spot files from 2025) are converted to ms. Rows with fewer than 11 columns are skipped."""
    text = _csv_text(blob)
    out: list[list[float]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 11 or not row[0].strip().isdigit():
            continue
        ts = int(row[0])
        if ts > 10**14:
            ts //= 1000
        out.append([float(ts)] + [float(row[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10)])
    out.sort(key=lambda r: r[0])
    return out


def parse_funding_zip(blob: bytes) -> list[list[float]]:
    """Rows ``[calc_time_ms, funding_interval_hours, funding_rate]`` from an archive ``fundingRate`` zip.

    Columns in the archive: ``calc_time, funding_interval_hours, last_funding_rate`` (rate as a decimal per interval;
    0.0001 = 0.01 %). Header optional; microsecond timestamps are converted to ms."""
    text = _csv_text(blob)
    out: list[list[float]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3 or not row[0].strip().isdigit():
            continue
        ts = int(row[0])
        if ts > 10**14:
            ts //= 1000
        out.append([float(ts), float(row[1] or 0), float(row[2])])
    out.sort(key=lambda r: r[0])
    return out


def parse_metrics_zip(blob: bytes) -> list[list[float]]:
    """Rows ``[create_time_ms, sum_open_interest, sum_open_interest_value]`` from a daily USD-M ``metrics`` zip.

    Archive columns: ``create_time, symbol, sum_open_interest`` (base units: coins/contracts of the symbol, e.g. BTC),
    ``sum_open_interest_value`` (USDT), then long/short and taker ratios (dropped). One row per 5 minutes, rows can be
    missing. The stamp's meaning changed between 2024-03-03 14:30 and 2024-03-04 00:45 UTC (measured on 6 383 flush
    events via value / OI vs the 1m price, docs/verification/2026-10-24-oi-confirmed-liquidation-flush.md); the switch
    is ASSUMED at 2024-03-04 00:00 UTC (file boundary), not checked on raw rows. Before, the snapshot is taken AT
    ``create_time``; from then on, ``create_time`` is the interval start and the snapshot is taken 5 min later (e.g.
    BTCUSDT 2025-10-10 cascade). Use
    ``analytics.oi_flush.archive_obs_times`` for observation times. Header optional; sorted by time."""
    text = _csv_text(blob)
    out: list[list[float]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4 or not row[0][:1].isdigit():
            continue
        try:
            out.append([float(_epoch_s(row[0]) * 1000), float(row[2]), float(row[3])])
        except ValueError as e:
            raise ProviderError("binance_vision", "backend_error", f"bad metrics csv: {e}") from None
    out.sort(key=lambda r: r[0])
    return out


METRICS_COLUMNS = (
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


def parse_metrics_ratios_zip(blob: bytes) -> list[list[float]]:
    """Rows ``[create_time_ms, *METRICS_COLUMNS]`` from a daily USD-M ``metrics`` zip (every numeric column).

    Long/short ratios are long / short (1.25 = 1.25 longs per short): ``count_long_short_ratio`` counts all accounts,
    ``count_toptrader_long_short_ratio`` counts top-trader accounts, ``sum_toptrader_long_short_ratio`` weights top
    traders by position; ``sum_taker_long_short_vol_ratio`` is taker buy / sell volume. Columns are found by the
    header when there is one (archive order otherwise); an empty cell becomes NaN. Same ``create_time`` semantics as
    ``parse_metrics_zip`` (observation time: ``analytics.oi_flush.archive_obs_times``). Sorted by time."""
    text = _csv_text(blob)
    idx = list(range(2, 2 + len(METRICS_COLUMNS)))
    out: list[list[float]] = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if not row[0][:1].isdigit():
            names = [c.strip() for c in row]
            if all(c in names for c in METRICS_COLUMNS):
                idx = [names.index(c) for c in METRICS_COLUMNS]
            continue
        if len(row) <= max(idx):
            continue
        try:
            vals = [float(row[i]) if row[i].strip() else float("nan") for i in idx]
            out.append([float(_epoch_s(row[0]) * 1000), *vals])
        except ValueError as e:
            raise ProviderError("binance_vision", "backend_error", f"bad metrics csv: {e}") from None
    out.sort(key=lambda r: r[0])
    return out


def _epoch_s(stamp: str) -> int:
    s = stamp.strip()
    if s.isdigit():  # epoch ms or us in some archive generations
        v = int(s)
        return v // 1_000_000 if v > 10**14 else v // 1000
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


def parse_book_depth_zip(blob: bytes) -> dict[str, np.ndarray]:
    """Columns of a daily USD-M ``bookDepth`` zip: ``ts`` (snapshot time, epoch seconds UTC), ``pct`` (signed distance
    from the price in percent: -1 = bids within 1 % below, +1 = asks within 1 % above), ``depth`` (cumulative base
    quantity) and ``notional`` (cumulative quote value, USDT). Snapshots every ~30 s; levels +-1..5 (and +-0.2 in newer
    files). Header optional."""
    lines = [ln for ln in _csv_text(blob).splitlines() if ln and ln[0].isdigit()]  # drop header / blank lines
    if not lines:
        return {"ts": np.empty(0, dtype=np.int64), "pct": np.empty(0), "depth": np.empty(0), "notional": np.empty(0)}
    fields = ",".join(lines).split(",")
    if len(fields) != 4 * len(lines):
        raise ProviderError("binance_vision", "backend_error", "bad bookDepth csv: expected 4 columns per row")
    try:
        vals = np.array([fields[1::4], fields[2::4], fields[3::4]], dtype=float)
    except ValueError as e:
        raise ProviderError("binance_vision", "backend_error", f"bad bookDepth csv: {e}") from None
    seen: dict[str, int] = {}
    heads = fields[0::4]
    ts = np.fromiter((seen[h] if h in seen else seen.setdefault(h, _epoch_s(h)) for h in heads), dtype=np.int64, count=len(heads))
    return {"ts": ts, "pct": vals[0], "depth": vals[1], "notional": vals[2]}


def parse_listing(blob: bytes) -> tuple[list[str], list[str], str | None]:
    """(common prefixes, keys, next marker or None) from one S3 ListBucket XML page."""
    try:
        root = ET.fromstring(blob)
    except ET.ParseError as e:
        raise ProviderError("binance_vision", "backend_error", f"bad listing xml: {e}") from None
    common = [p.findtext(f"{_S3_NS}Prefix") or "" for p in root.iter(f"{_S3_NS}CommonPrefixes")]
    keys = [k.findtext(f"{_S3_NS}Key") or "" for k in root.iter(f"{_S3_NS}Contents")]
    truncated = (root.findtext(f"{_S3_NS}IsTruncated") or "").lower() == "true"
    nxt = root.findtext(f"{_S3_NS}NextMarker") or (keys[-1] if keys else (common[-1] if common else None))
    return [c for c in common if c], [k for k in keys if k], (nxt if truncated else None)


def _check(symbol: str, interval: str) -> None:
    if not _SYMBOL.match(symbol):
        raise ProviderError("binance_vision", "config", f"bad symbol {symbol!r}")
    if interval not in VALID_INTERVALS:
        raise ProviderError("binance_vision", "config", f"interval must be one of {VALID_INTERVALS}")


def _check_day(day: str) -> None:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise ProviderError("binance_vision", "config", f"bad day {day!r}")


class BinanceVision:
    name = "binance_vision"

    def __init__(self, files: Http, listing: Http) -> None:
        self.http = files
        self.listing_http = listing

    async def list_dir(self, prefix: str, *, max_pages: int = 50) -> Fetched:
        """All sub-directories and keys under ``prefix`` (walks S3 pages). Data: ``{"dirs": [...], "keys": [...]}``."""
        dirs: list[str] = []
        keys: list[str] = []
        marker: str | None = ""
        ev = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"delimiter": "/", "prefix": prefix}
            if marker:
                params["marker"] = marker
            f = await self.listing_http.get_bytes(f"/{BUCKET}", params=params, tool="s3_list")
            ev = ev or f.evidence
            d, k, marker = parse_listing(f.data)
            dirs += d
            keys += k
            if not marker:
                break
        assert ev is not None
        return Fetched({"dirs": dirs, "keys": keys}, ev)

    async def um_symbols(self) -> Fetched:
        """Every USDT-M symbol that ever had monthly klines in the archive (including delisted ones)."""
        f = await self.list_dir(f"{UM_KLINES}/monthly/klines/")
        syms = sorted({d.rstrip("/").rsplit("/", 1)[-1] for d in f.data["dirs"]})
        return Fetched(syms, f.evidence)

    async def um_klines_monthly(self, symbol: str, interval: str, year: int, month: int) -> Fetched:
        """One month of USDT-M klines; ``data_unavailable`` (404) when the symbol did not trade that month."""
        _check(symbol, interval)
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/monthly/klines/{symbol}/{interval}/{name}", tool="um_klines_monthly")
        return Fetched(parse_klines_zip(f.data), f.evidence)

    async def um_klines_daily(self, symbol: str, interval: str, day: str) -> Fetched:
        """One UTC day (``YYYY-MM-DD``) of USDT-M klines."""
        _check(symbol, interval)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            raise ProviderError(self.name, "config", f"bad day {day!r}")
        name = f"{symbol}-{interval}-{day}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/daily/klines/{symbol}/{interval}/{name}", tool="um_klines_daily")
        return Fetched(parse_klines_zip(f.data), f.evidence)

    async def um_funding_monthly(self, symbol: str, year: int, month: int) -> Fetched:
        """One month of USDT-M funding settlements ``[calc_time_ms, interval_hours, rate]``; 404 -> ``data_unavailable``.

        Rates are decimals per funding interval (positive: longs pay shorts). The archive keeps delisted symbols."""
        _check(symbol, "1d")
        name = f"{symbol}-fundingRate-{year:04d}-{month:02d}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/monthly/fundingRate/{symbol}/{name}", tool="um_funding_monthly")
        return Fetched(parse_funding_zip(f.data), f.evidence)

    async def um_premium_index_monthly(self, symbol: str, interval: str, year: int, month: int) -> Fetched:
        """One month of USDT-M ``premiumIndexKlines``: kline rows whose prices are the premium index, a decimal
        (0.01 = perp impact price 1 % above the multi-venue spot index). Volume columns are 0; 404 -> ``data_unavailable``."""
        _check(symbol, interval)
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        f = await self.http.get_bytes(
            f"/{UM_KLINES}/monthly/premiumIndexKlines/{symbol}/{interval}/{name}", tool="um_premium_index_monthly"
        )
        return Fetched(parse_klines_zip(f.data), f.evidence)

    async def um_mark_price_monthly(self, symbol: str, interval: str, year: int, month: int) -> Fetched:
        """One month of USDT-M ``markPriceKlines``: kline rows whose prices are the exchange mark price (the median of
        the last price, the funding-adjusted index and the index plus a smoothed basis), in quote currency like klines.
        Volume columns are 0; 404 -> ``data_unavailable``."""
        _check(symbol, interval)
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/monthly/markPriceKlines/{symbol}/{interval}/{name}", tool="um_mark_price_monthly")
        return Fetched(parse_klines_zip(f.data), f.evidence)

    async def um_premium_index_daily(self, symbol: str, interval: str, day: str) -> Fetched:
        """One UTC day (``YYYY-MM-DD``) of USDT-M ``premiumIndexKlines`` (see ``um_premium_index_monthly``)."""
        _check(symbol, interval)
        _check_day(day)
        name = f"{symbol}-{interval}-{day}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/daily/premiumIndexKlines/{symbol}/{interval}/{name}", tool="um_premium_index_daily")
        return Fetched(parse_klines_zip(f.data), f.evidence)

    async def um_book_depth_daily(self, symbol: str, day: str) -> Fetched:
        """One UTC day of USDT-M ``bookDepth`` snapshots (see ``parse_book_depth_zip``); 404 -> ``data_unavailable``.

        The archive starts in 2023-01 (early files can be partial: count snapshots before use). ~0.5 MB per day."""
        _check(symbol, "1d")
        _check_day(day)
        name = f"{symbol}-bookDepth-{day}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/daily/bookDepth/{symbol}/{name}", tool="um_book_depth_daily")
        return Fetched(parse_book_depth_zip(f.data), f.evidence)

    async def um_metrics_daily(self, symbol: str, day: str) -> Fetched:
        """One UTC day of USDT-M ``metrics`` (5-min open interest, see ``parse_metrics_zip``); 404 -> ``data_unavailable``.

        Rows are observed at ``create_time`` before 2024-03-04 (assumed switch, see ``parse_metrics_zip``) and at
        ``create_time + 5 min`` after. Most alts start 2021-12."""
        _check(symbol, "1d")
        _check_day(day)
        name = f"{symbol}-metrics-{day}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/daily/metrics/{symbol}/{name}", tool="um_metrics_daily")
        return Fetched(parse_metrics_zip(f.data), f.evidence)

    async def um_metrics_ratios_daily(self, symbol: str, day: str) -> Fetched:
        """One UTC day of USDT-M ``metrics`` with every column (OI and long/short ratios, see
        ``parse_metrics_ratios_zip``); 404 -> ``data_unavailable``. Same file and time semantics as ``um_metrics_daily``."""
        _check(symbol, "1d")
        _check_day(day)
        name = f"{symbol}-metrics-{day}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/daily/metrics/{symbol}/{name}", tool="um_metrics_ratios_daily")
        return Fetched(parse_metrics_ratios_zip(f.data), f.evidence)

    async def spot_symbols(self) -> Fetched:
        """Every spot symbol that ever had monthly klines in the archive (including delisted pairs)."""
        f = await self.list_dir(f"{SPOT}/monthly/klines/", max_pages=200)
        syms = sorted({d.rstrip("/").rsplit("/", 1)[-1] for d in f.data["dirs"]})
        return Fetched(syms, f.evidence)

    async def spot_klines_monthly(self, symbol: str, interval: str, year: int, month: int) -> Fetched:
        """One month of spot klines (same row layout as USDT-M; files from 2025 carry microsecond timestamps)."""
        _check(symbol, interval)
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        f = await self.http.get_bytes(f"/{SPOT}/monthly/klines/{symbol}/{interval}/{name}", tool="spot_klines_monthly")
        return Fetched(parse_klines_zip(f.data), f.evidence)

    async def spot_klines_daily(self, symbol: str, interval: str, day: str) -> Fetched:
        """One UTC day (``YYYY-MM-DD``) of spot klines."""
        _check(symbol, interval)
        _check_day(day)
        name = f"{symbol}-{interval}-{day}.zip"
        f = await self.http.get_bytes(f"/{SPOT}/daily/klines/{symbol}/{interval}/{name}", tool="spot_klines_daily")
        return Fetched(parse_klines_zip(f.data), f.evidence)

    async def um_klines_taker_monthly(self, symbol: str, interval: str, year: int, month: int) -> Fetched:
        """One month of USDT-M klines with taker-buy columns (``parse_klines_taker_zip`` rows); 404 -> ``data_unavailable``."""
        _check(symbol, interval)
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        f = await self.http.get_bytes(f"/{UM_KLINES}/monthly/klines/{symbol}/{interval}/{name}", tool="um_klines_monthly")
        return Fetched(parse_klines_taker_zip(f.data), f.evidence)

    async def spot_klines_taker_monthly(self, symbol: str, interval: str, year: int, month: int) -> Fetched:
        """One month of spot klines with taker-buy columns (``parse_klines_taker_zip`` rows); 404 -> ``data_unavailable``."""
        _check(symbol, interval)
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        f = await self.http.get_bytes(f"/{SPOT}/monthly/klines/{symbol}/{interval}/{name}", tool="spot_klines_monthly")
        return Fetched(parse_klines_taker_zip(f.data), f.evidence)

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.listing_http.aclose()
