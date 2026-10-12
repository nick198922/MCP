from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.alerts.engine import evaluate_dsl_rules
from tradedesk.config import Settings
from tradedesk.rules.dsl import DslError, compile_expr, evaluate
from tradedesk.rules.metrics import MetricContext, build_metrics
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store import Store

# ---------- pure DSL ----------


async def test_dsl_arithmetic_and_bools():
    assert await evaluate("1 + 2 * 3", {}) == 7
    assert await evaluate("not (3 > 10)", {}) is True
    assert await evaluate("1 == 1 and 2 == 2", {}) is True
    assert await evaluate("1 == 1 and 2 == 3", {}) is False
    assert await evaluate("x > 0", {"names": {"x": 5}}) is True


async def test_dsl_async_function_call():
    async def half(a):
        return a / 2.0

    env = {"calls": {"half": half}}
    assert await evaluate("half(10) + half(4)", env) == pytest.approx(7.0)


def test_dsl_rejects_dangerous_syntax():
    for src in [
        "__import__('os')",
        "a.b",
        "d['k']",
        "[x for x in [1,2]]",
        "lambda x: x",
        "a or (b := 1)",
    ]:
        with pytest.raises(DslError):
            compile_expr(src)


async def test_dsl_rejects_unknown_name_and_fn():
    with pytest.raises(DslError):
        await evaluate("missing_name > 0", {})
    with pytest.raises(DslError):
        await evaluate("boom(1)", {"calls": {}})


# ---------- metrics bound to Store ----------


async def test_metrics_funding_and_zscore():
    s = Store(":memory:")
    await s.init()
    now_ts = int(NOW.timestamp())
    for i in range(10):
        await s.insert_funding(
            ts=now_ts - (9 - i) * 3600,
            rows=[
                {"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0001, "interval_hours": 8.0, "annualized": 0.08 + 0.01 * i},
            ],
        )
    ctx = MetricContext(store=s, now_ts=now_ts)
    calls = build_metrics(ctx)
    # latest = 0.17
    assert await calls["funding"]("binanceusdm", "BTC") == pytest.approx(0.17)
    mean = await calls["funding_mean"]("binanceusdm", "BTC", 1)
    assert mean == pytest.approx(sum(0.08 + 0.01 * i for i in range(10)) / 10)
    z = await calls["funding_zscore"]("binanceusdm", "BTC", 1)
    assert z is not None and z > 0.0  # latest sits above the mean
    await s.aclose()


# ---------- engine ----------


async def test_evaluate_dsl_rules_fires_and_respects_cooldown():
    s = Store(":memory:")
    await s.init()
    now_ts = int(NOW.timestamp())
    await s.insert_funding(
        ts=now_ts,
        rows=[{"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0003, "interval_hours": 8.0, "annualized": 0.32}],
    )
    rid = await s.rule_insert(
        name="hot_funding",
        expr='funding("binanceusdm","BTC") > 0.30',
        message="hot {value_pct:.1f}% ann",
        cooldown_seconds=600,
        enabled=True,
        created_ts=now_ts,
    )
    assert rid > 0
    fired = await evaluate_dsl_rules(s, now_ts=now_ts)
    assert len(fired) == 1
    # Record the alert so cooldown kicks in.
    await s.insert_alert(rule_id="db:hot_funding", ts=now_ts, payload={}, channel="log", delivered=True)
    fired2 = await evaluate_dsl_rules(s, now_ts=now_ts + 60)
    assert fired2 == []
    # Past cooldown, fires again.
    fired3 = await evaluate_dsl_rules(s, now_ts=now_ts + 700)
    assert len(fired3) == 1
    await s.aclose()


async def test_evaluate_dsl_rules_swallows_bad_expr():
    s = Store(":memory:")
    await s.init()
    now_ts = int(NOW.timestamp())
    # Deliberately broken expression (unknown fn).
    await s.rule_insert(
        name="bad",
        expr="unknown_fn(1,2)",
        message="",
        cooldown_seconds=0,
        enabled=True,
        created_ts=now_ts,
    )
    fired = await evaluate_dsl_rules(s, now_ts=now_ts)
    assert fired == []  # engine must not crash on a bad user expression
    await s.aclose()


# ---------- MCP tools round-trip ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "rules.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_rules_add_list_test_disable(state, tmp_path):
    rt = await _rt(state, tmp_path)
    now_ts = int(NOW.timestamp())
    await rt.store.insert_funding(
        ts=now_ts,
        rows=[{"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0003, "interval_hours": 8.0, "annualized": 0.42}],
    )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "rules_test")
        assert "available_metrics" in env["data"]

        bad = await _call(srv, "rules_test", expr="1 + ")
        assert bad["ok"] is False

        good = await _call(srv, "rules_test", expr='funding("binanceusdm","BTC") > 0.30')
        assert good["data"]["parsed"] is True and good["data"]["truthy"] is True

        async with Client(srv) as c:
            raw = await c.call_tool(
                "rules_add",
                {
                    "name": "hot_btc",
                    "expr": 'funding("binanceusdm","BTC") > 0.30',
                    "message": "hot funding",
                    "cooldown_seconds": 600,
                },
            )
        add = raw.structured_content or json.loads(raw.content[0].text)
        if set(add) == {"result"}:
            add = add["result"]
        assert add["data"]["id"] >= 1
        rid = add["data"]["id"]

        lst = await _call(srv, "rules_list")
        assert lst["data"]["count"] == 1 and lst["data"]["rules"][0]["name"] == "hot_btc"

        disabled = await _call(srv, "rules_disable", id=rid, enable=False)
        assert disabled["data"]["enabled"] is False

        deleted = await _call(srv, "rules_disable", id=rid, delete=True)
        assert deleted["data"]["deleted"] is True
    finally:
        await rt.aclose()


async def test_rules_presets_load_installs_bundled(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "rules_presets_load")
        d = env["data"]
        assert d["candidates"] >= 6
        assert d["inserted"] == d["candidates"]
        assert d["failed"] == []
        # Idempotent — second call skips everything.
        again = await _call(srv, "rules_presets_load")
        assert again["data"]["inserted"] == 0
        listed = (await _call(srv, "rules_list"))["data"]["rules"]
        assert sorted(again["data"]["already_present"]) == sorted(r["name"] for r in listed)
    finally:
        await rt.aclose()


async def test_rules_presets_load_subset_and_force_enable(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "rules_presets_load",
            only=["preset_eth_funding_hot"],
            enable_disabled=True,
        )
        d = env["data"]
        assert d["candidates"] == 1 and d["inserted"] == 1
        rows = (await _call(srv, "rules_list"))["data"]["rules"]
        assert rows[0]["name"] == "preset_eth_funding_hot" and rows[0]["enabled"] is True
    finally:
        await rt.aclose()
