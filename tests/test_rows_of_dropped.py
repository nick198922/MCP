"""`_rows_of` in tools/cycle.py and tools/flow.py: `dropped_rows` gets its own warning (item 213)."""

from __future__ import annotations

import pytest

from test_market_flow import _flow_rt
from test_server import call
from tradedesk.core.envelope import Builder, Fetched
from tradedesk.server import build_server
from tradedesk.tools import cycle, flow

ROWS = [{"timestamp": 1, "fundingRate": 0.0001}, {"timestamp": 2, "fundingRate": 0.0002}]
HELPERS = [cycle._rows_of, flow._rows_of]


@pytest.mark.parametrize("rows_of", HELPERS)
def test_complete_range_has_no_warning(rows_of):
    for got in (
        {"rows": ROWS, "complete": True},
        {"rows": ROWS, "complete": True, "truncated": False, "dropped_rows": 0},
    ):
        b = Builder()
        assert rows_of(b, "funding", got) == ROWS
        assert b.warnings == []


@pytest.mark.parametrize("rows_of", HELPERS)
def test_incomplete_without_dropped_rows_keeps_the_paging_warning(rows_of):
    for got in (
        {"rows": ROWS, "complete": False},  # klines / OI / Coinbase walks: no `truncated` key
        {"rows": ROWS, "complete": False, "truncated": True, "dropped_rows": 0},
    ):
        b = Builder()
        assert rows_of(b, "spot_klines", got) == ROWS
        assert b.warnings == ["spot_klines: history incomplete (paging stopped early); the earliest part of the window may be missing."]


@pytest.mark.parametrize("rows_of", HELPERS)
def test_dropped_rows_warn_with_the_count_and_not_as_paging(rows_of):
    b = Builder()
    got = {"rows": ROWS, "complete": False, "truncated": False, "dropped_rows": 2}
    assert rows_of(b, "funding:bybit", got) == ROWS
    assert len(b.warnings) == 1
    w = b.warnings[0]
    assert w.startswith("funding:bybit: 2 malformed rows dropped") and "paging" not in w


@pytest.mark.parametrize("rows_of", HELPERS)
def test_dropped_rows_and_truncated_give_both_warnings(rows_of):
    b = Builder()
    got = {"rows": ROWS, "complete": False, "truncated": True, "dropped_rows": 3}
    assert rows_of(b, "funding", got) == ROWS
    assert len(b.warnings) == 2
    assert b.warnings[0].startswith("funding: 3 malformed rows dropped")
    assert "paging stopped early" in b.warnings[1]


@pytest.mark.parametrize("rows_of", HELPERS)
@pytest.mark.parametrize("bad", [True, "2", None, -1, 1.5, [2]])
def test_wrong_type_dropped_rows_is_ignored(rows_of, bad):
    b = Builder()
    assert rows_of(b, "funding", {"rows": ROWS, "complete": True, "dropped_rows": bad}) == ROWS
    assert b.warnings == []


async def _market_flow(patch_bybit: bool) -> dict:
    rt = _flow_rt()
    if patch_bybit:
        orig = rt.exchanges.funding_rate_history_range

        async def dropped(ex_id, *a, **kw):
            f = await orig(ex_id, *a, **kw)
            if ex_id == "bybit":
                return Fetched({**f.data, "complete": False, "truncated": False, "dropped_rows": 2}, f.evidence)
            return f

        rt.exchanges.funding_rate_history_range = dropped
    srv, _ = build_server(rt)
    env = await call(srv, "market_flow", base="BTC", timeframe="1h", days=1)
    await rt.aclose()
    return env


async def test_market_flow_names_dropped_funding_rows_and_keeps_the_numbers():
    clean = await _market_flow(False)
    env = await _market_flow(True)
    assert clean["ok"] and env["ok"] and env["missing"] == []
    assert not any("malformed rows dropped" in w or "paging stopped" in w for w in clean["warnings"])
    hits = [w for w in env["warnings"] if w.startswith("funding:bybit: 2 malformed rows dropped")]
    assert len(hits) == 1
    assert not any("funding:bybit: history incomplete" in w for w in env["warnings"])
    assert env["data"]["venues_latest"] == clean["data"]["venues_latest"]
    assert env["data"]["summary"] == clean["data"]["summary"]
