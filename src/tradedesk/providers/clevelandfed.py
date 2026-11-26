"""Federal Reserve Bank of Cleveland inflation nowcast chart files (public, no key).

The nowcasting page (``/indicators-and-data/inflation-nowcasting``) draws its charts from three JSON files under
``/-/media/files/webcharts/inflationnowcasting/``: ``nowcast_month.json`` (MoM, percent), ``nowcast_quarter.json`` and
``nowcast_year.json`` (YoY, percent). Each is a list of FusionCharts charts, one per target month since 2013-07, with the
daily nowcasts of that month (~7.7 MB each, 26.11.2026); ``chart._comment`` of the charts is the file's stamp
('2026-11-25 00:00'). The model updates the files once a working day at about 10:00 ET. Parsing of the charts lives in
``analytics/pm_cpi.py``. All text is untrusted third-party data.

Used by the collector job ``pm_cpi_snapshot`` (collector/pm_cpi_snapshot.py); no MCP tool reads it.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://www.clevelandfed.org"
NOWCAST_PATHS = {
    "year": "/-/media/files/webcharts/inflationnowcasting/nowcast_year.json",
    "month": "/-/media/files/webcharts/inflationnowcasting/nowcast_month.json",
}
# No published limit; two files a few times a day. One request every 2 s at most, and a long timeout for 7.7 MB bodies.
RATE_PER_SECOND = 0.5
BURST = 2
TIMEOUT_S = 60.0
_TARGET = re.compile(r"^\d{4}-\d{1,2}$")


def shape_problem(data: Any) -> str | None:
    """Why a decoded nowcast file is unusable (None = fine): it must be a non-empty list whose last chart has a
    'YYYY-M' ``chart.subcaption``, a ``categories[0].category`` list and a ``dataset`` list."""
    if not isinstance(data, list) or not data:
        return "nowcast file is not a non-empty list of charts"
    last = data[-1]
    try:
        ok = (
            isinstance(last["chart"]["subcaption"], str)
            and _TARGET.match(last["chart"]["subcaption"]) is not None
            and isinstance(last["categories"][0]["category"], list)
            and isinstance(last["dataset"], list)
        )
    except (KeyError, TypeError, IndexError):
        ok = False
    return None if ok else "unexpected nowcast chart shape (chart.subcaption / categories / dataset)"


class ClevelandFed:
    name = "clevelandfed"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def nowcast(self, kind: str) -> Fetched:
        """One nowcast file, ``kind`` 'year' (YoY) or 'month' (MoM). ``data`` = {"raw": the body as received (bytes),
        "charts": the decoded list (None when the body is not JSON), "problem": why the body is unusable (None =
        fine)}. A body that arrived but does not parse is returned, not raised, so the caller can still keep its bytes
        (review C of item 480, remark 10); a transport or HTTP failure raises ``ProviderError`` as usual. The ~7.7 MB
        body is decoded in a worker thread (``asyncio.to_thread``: ~0.15 s off the event loop). Never cached: the caller
        needs today's file."""
        path = NOWCAST_PATHS.get(kind)
        if path is None:
            raise ProviderError(self.name, "config", f"unknown nowcast file {kind!r} (year | month)")
        f = await self.http.get_bytes(path, tool=f"nowcast_{kind}")
        raw = bytes(f.data)
        charts, problem = await asyncio.to_thread(_decode, raw)
        return Fetched({"raw": raw, "charts": charts, "problem": problem}, f.evidence)


def _decode(raw: bytes) -> tuple[Any, str | None]:
    """(decoded body or None, problem or None) of one nowcast file; never raises."""
    try:
        charts = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, f"body is not JSON ({len(raw)} bytes)"
    return charts, shape_problem(charts)
