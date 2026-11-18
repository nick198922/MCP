"""Compact default answers (BACKLOG «великі відповіді за замовчуванням», standing card item 379).

The default call of `market_flow` and `hyperliquid_funding_history` stays small (<= 8 000 characters of JSON) so it
does not flood a chat or an agent's context; the full per-bar / per-payment series comes only with
`include_series=true`. Fakes only (tests/conftest.py, the captured market_flow responses of 2026-10-28). No network.
"""

from __future__ import annotations

import json

from test_market_flow import _flow_rt
from test_server import call
from tradedesk.server import build_server

MAX_DEFAULT_CHARS = 8_000


def _size(env: dict) -> int:
    return len(json.dumps(env, ensure_ascii=False))


async def test_market_flow_default_is_compact_and_series_is_opt_in():
    rt = _flow_rt()
    srv, _ = build_server(rt)
    default = await call(srv, "market_flow", base="BTC")  # 1h, 7 days: the call a model makes without arguments
    full = await call(srv, "market_flow", base="BTC", include_series=True)
    await rt.aclose()
    assert default["ok"] and full["ok"]
    assert "series" not in default["data"] and default["data"]["summary"]["24h"] is not None
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert len(full["data"]["series"]) == 7 * 24  # every closed 1h bar of the window
    assert {k: v for k, v in full["data"].items() if k != "series"} == default["data"]  # same summaries either way
    assert _size(full) > MAX_DEFAULT_CHARS


async def test_hyperliquid_funding_history_default_is_compact_and_entries_are_opt_in(server):
    default = await call(server, "hyperliquid_funding_history", coin="BTC")  # 30 days of hourly payments
    full = await call(server, "hyperliquid_funding_history", coin="BTC", include_series=True)
    assert default["ok"] and full["ok"]
    assert "entries" not in default["data"] and default["data"]["stats"]["count"] > 0
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    entries = full["data"]["entries"]
    assert len(entries) == full["data"]["stats"]["count"] == default["data"]["stats"]["count"]
    assert all(set(e) == {"ts_ms", "rate"} for e in entries)
    assert full["data"]["stats"] == default["data"]["stats"]
