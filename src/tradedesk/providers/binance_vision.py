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
from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

FILES_URL = "https://data.binance.vision"
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com"
BUCKET = "data.binance.vision"
UM_KLINES = "data/futures/um"
VALID_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d")
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
# Archive symbols are mostly [A-Z0-9] but some bases are not Latin (e.g. a CJK meme coin); forbid only path syntax.
_SYMBOL = re.compile(r"^[^/\\.?&#%\s]+$")


def parse_klines_zip(blob: bytes) -> list[list[float]]:
    """Rows ``[open_ms, open, high, low, close, base_volume, quote_volume]`` from an archive kline zip.

    Handles files with and without a header row and microsecond timestamps (newer archives)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            raise ProviderError("binance_vision", "backend_error", "zip without csv")
        text = zf.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile as e:
        raise ProviderError("binance_vision", "backend_error", f"bad zip: {e}") from None
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

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.listing_http.aclose()
