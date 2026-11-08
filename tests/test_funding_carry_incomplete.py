"""funding_carry_scan marks a symbol whose funding history came back incomplete (complete=False).

Sibling of tests/test_funding_spread_incomplete.py (item 212): the trailing APR of a partial history may be wrong, so the
symbol is flagged `incomplete_history`, listed in `summary.incomplete_history` and kept out of
`summary.above_entry_with_spot`; its numbers and `status` are unchanged.
"""

from __future__ import annotations

from typing import Any

from conftest import NOW, FakeEx, State, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.core.envelope import Fetched
from tradedesk.core.errors import ProviderError
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

ARGS = {"venue": "binanceusdm", "bases": ["btc"], "entry_apr": 0.05, "exit_apr": 0.0}


class _BadRowEx(FakeEx):
    """Funding history with one row whose timestamp is null: the provider drops it and reports complete=False."""

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
        return [*rows[:-1], {**rows[-1], "timestamp": None}]


def _runtime(state: State, factory=FakeEx) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=factory,
    )
    rt.now = lambda: NOW
    return rt


def _incomplete_warnings(env: dict) -> list[str]:
    return [w for w in env["warnings"] if "funding history incomplete" in w]


async def test_complete_history_is_not_flagged(state) -> None:
    rt = _runtime(state)
    try:
        env = await call(build_server(rt)[0], "funding_carry_scan", **ARGS)
        assert env["ok"], env["missing"]
        (row,) = env["data"]["rows"]
        assert row["status"] == "above_entry" and row["incomplete_history"] is False
        summary = env["data"]["summary"]
        assert summary["above_entry_with_spot"] == ["BTC"] and summary["incomplete_history"] == []
        assert _incomplete_warnings(env) == []
    finally:
        await rt.aclose()


async def test_dropped_row_flags_the_symbol_and_keeps_it_out_of_the_spot_list(state) -> None:
    rt = _runtime(state, _BadRowEx)
    try:
        env = await call(build_server(rt)[0], "funding_carry_scan", **ARGS)
        assert env["ok"], env["missing"]
        (row,) = env["data"]["rows"]
        assert row["incomplete_history"] is True
        assert row["status"] == "above_entry" and row["spot_available"] is True  # numbers stay, only the verdict lists change
        summary = env["data"]["summary"]
        assert summary["above_entry"] == ["BTC"]
        assert summary["above_entry_with_spot"] == [] and summary["incomplete_history"] == ["BTC"]
        (w,) = _incomplete_warnings(env)
        assert w.startswith("binanceusdm BTC/USDT:USDT: funding history incomplete") and "above_entry_with_spot" in w
    finally:
        await rt.aclose()


async def test_only_the_cut_symbol_is_flagged(state) -> None:
    rt = _runtime(state)
    orig = rt.exchanges.funding_rate_history_range

    async def cut_eth(ex_id: str, symbol: str, *a: Any, **kw: Any) -> Fetched:
        f = await orig(ex_id, symbol, *a, **kw)
        return Fetched({**f.data, "complete": False, "truncated": True}, f.evidence) if symbol.startswith("ETH/") else f

    rt.exchanges.funding_rate_history_range = cut_eth  # type: ignore[method-assign]
    try:
        env = await call(build_server(rt)[0], "funding_carry_scan", **{**ARGS, "bases": ["BTC", "ETH"]})
        assert env["ok"], env["missing"]
        flags = {x["base"]: x["incomplete_history"] for x in env["data"]["rows"]}
        assert flags == {"BTC": False, "ETH": True}
        assert env["data"]["summary"]["incomplete_history"] == ["ETH"]
        assert env["data"]["summary"]["above_entry_with_spot"] == ["BTC"]
        assert [w.split(":")[0] for w in _incomplete_warnings(env)] == ["binanceusdm ETH/USDT"]
    finally:
        await rt.aclose()


async def test_history_failure_goes_to_missing_not_incomplete(state) -> None:
    rt = _runtime(state)

    async def boom(*a: Any, **kw: Any) -> Fetched:
        raise ProviderError("ccxt:binanceusdm", "network", "down")

    rt.exchanges.funding_rate_history_range = boom  # type: ignore[method-assign]
    try:
        env = await call(build_server(rt)[0], "funding_carry_scan", **ARGS)
        assert any(m["field"] == "funding_history:BTC/USDT:USDT" for m in env["missing"])
        # the current funding still gives a row; a failed fetch is a missing source, not a partial history
        for row in (env["data"] or {}).get("rows", []):
            assert row["incomplete_history"] is False
        assert _incomplete_warnings(env) == []
    finally:
        await rt.aclose()
