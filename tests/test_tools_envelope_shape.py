"""End-to-end check of the Envelope shape of successful tool answers (CLAUDE.md invariants 1-2, core/envelope.py).

Complements tests/test_tools_never_raise.py and tests/test_tools_provider_outage.py, which check that failing providers
still give an envelope. Here every registered tool is called through a real MCP client against the happy-path fakes of
tests/conftest.py (Deribit / Hyperliquid / Binance / Bybit / Polymarket / CoinGecko / ... transports and FakeEx);
providers without a fake answer 404, and nothing reaches the real network. Arguments and SKIP come from
tests/test_tools_never_raise.py. For every answer with ok=true it checks:
- the keys and types of the Envelope: ok (bool), data, evidence / missing / warnings (lists), as_of (ISO time with zone);
- every `evidence` item names its provider and its fetch time (ISO with zone); every `missing` item has field,
  provider and reason; every warning is a string;
- a tool that made a provider call has at least one `evidence` item (otherwise the numbers lose their source);
- ok=true with a non-empty `missing` comes with at least one warning;
- a tool that answers ok=true with no provider call at all (calculators, the local store) is listed in NO_NETWORK.
Answers with ok=false on these fakes (no fake for the provider, an empty store) are not checked here; they are counted
and named in the assertion message when too many tools fall out.
KNOWN: violations found and not fixed yet (each one also in docs/BACKLOG.md 6.47); a stale entry fails the test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import patch

import httpx
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from test_server import EXPECTED_TOOLS
from test_tools_never_raise import ARGS as NEVER_RAISE_ARGS
from test_tools_never_raise import SKIP
from test_tools_provider_outage import as_envelope
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# events_add needs a time (ts_ms or date) to succeed; the outage tests only need it to reach the store.
ARGS: dict[str, dict] = {**NEVER_RAISE_ARGS, "events_add": {**NEVER_RAISE_ARGS["events_add"], "date": "2026-11-05"}}

ENVELOPE_TYPES: dict[str, type] = {"ok": bool, "evidence": list, "missing": list, "warnings": list, "as_of": str}

# Tools that answer ok=true without any provider call, so an empty `evidence` is right for them.
NO_NETWORK: set[str] = {
    # pure calculators
    "fib_retracement",
    "odds_devig",
    "option_calc",
    "strategy_cash_and_carry",
    "strategy_funding_carry",
    "strategy_iron_condor",
    "strategy_payoff",
    "strategy_risk_reversal",
    "strategy_scenario_matrix",
    "strategy_straddle",
    "strategy_strangle",
    "touch_probability",
    # the local store (SQLite), the bundled event seed and the in-process health registry
    "alerts_recent",
    "assistant_paper_status",
    "events_add",
    "events_list",
    "events_seed_load",
    "forecast_log_status",
    "journal_add",
    "journal_close",
    "journal_list",
    "journal_stats",
    "liquidations_summary",
    "paper_carry_status",
    "plan_add",
    "plan_link",
    "plan_list",
    "positions_add",
    "positions_close",
    "positions_list",
    "rules_add",
    "rules_disable",
    "rules_list",
    "rules_presets_load",
    "rules_test",
    "system_health",
}

# Violations found and not fixed yet (also in docs/BACKLOG.md 6.47). Format: {"tool": "what is wrong"}.
_OKX_PARTIAL = "ok=true, one venue (okx) in `missing`, no warning: decide whether `missing` alone is enough for a partial answer"
KNOWN: dict[str, str] = {
    "perps_basis_compare": _OKX_PARTIAL,
    "perps_funding_compare": _OKX_PARTIAL,
    "perps_funding_history": _OKX_PARTIAL,
    "perps_open_interest": _OKX_PARTIAL,
}


def _not_mocked(req: httpx.Request) -> httpx.Response:
    return httpx.Response(404, text="not mocked")


class _Transports(dict):
    """The conftest fakes by provider name; every other Http the Runtime builds gets a 404 transport (no real network)."""

    def get(self, key, default=None):  # noqa: ARG002 - dict.get signature
        return super().get(key) or httpx.MockTransport(_not_mocked)


class _CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._calls.append(f"http:{request.url.host}{request.url.path}")
        return await self._inner.handle_async_request(request)


class _CountingEx:
    """FakeEx that records every async call (attributes FakeEx lacks stay missing)."""

    def __init__(self, ex_id: str, calls: list[str]) -> None:
        self._ex = FakeEx(ex_id)
        self._calls = calls
        self.id = ex_id
        self.has = self._ex.has

    def __getattr__(self, name: str):
        attr = getattr(self._ex, name)
        if name == "close" or not callable(attr):
            return attr

        async def counted(*a, **k):
            self._calls.append(f"ccxt:{self.id}.{name}")
            return await attr(*a, **k)

        return counted


def _runtime(tmp_path, calls: list[str]) -> Runtime:
    fakes = make_transports(State())
    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "envelope_shape.sqlite")),
        transports=_Transports({name: _CountingTransport(t, calls) for name, t in fakes.items()}),
        exchange_factory=lambda ex_id: _CountingEx(ex_id, calls),
    )
    rt.now = lambda: NOW
    return rt


def _is_iso_with_zone(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def shape_problems(env: Any, made_calls: bool) -> list[str]:
    """What is wrong with one ok=true answer (an empty list: the shape is right)."""
    if not isinstance(env, dict):
        return [f"not a dict: {str(env)[:120]}"]
    out = [f"no `{k}`" for k in (*ENVELOPE_TYPES, "data") if k not in env]
    out += [f"`{k}` is {type(env[k]).__name__}" for k, t in ENVELOPE_TYPES.items() if k in env and not isinstance(env[k], t)]
    if out:
        return out
    if not _is_iso_with_zone(env["as_of"]):
        out.append(f"as_of {env['as_of']!r} is not an ISO time with a zone")
    for i, ev in enumerate(env["evidence"]):
        if not isinstance(ev, dict) or not isinstance(ev.get("provider"), str) or not ev["provider"]:
            out.append(f"evidence[{i}] has no provider: {str(ev)[:120]}")
        elif not _is_iso_with_zone(ev.get("fetched_at")):
            out.append(f"evidence[{i}] ({ev['provider']}) has no fetch time: {ev.get('fetched_at')!r}")
    for i, m in enumerate(env["missing"]):
        if not isinstance(m, dict) or not all(isinstance(m.get(k), str) and m[k] for k in ("field", "provider", "reason")):
            out.append(f"missing[{i}] lacks field/provider/reason: {str(m)[:120]}")
    if not all(isinstance(w, str) for w in env["warnings"]):
        out.append("a warning is not a string")
    if made_calls and not env["evidence"]:
        out.append("ok=true after provider calls, but `evidence` is empty")
    if env["missing"] and not env["warnings"]:
        out.append(f"ok=true with {len(env['missing'])} `missing` item(s) and no warning")
    return out


async def check_every_tool(srv, calls: list[str]) -> dict[str, Any]:
    """Call each registered tool once; returns the counts, the ok=true tools without calls and the problems by tool."""
    report: dict[str, Any] = {"listed": 0, "called": 0, "ok": 0, "not_ok": [], "no_network": set(), "problems": {}}

    async def no_real_network(transport, request):
        raise AssertionError(f"real network: {request.method} {request.url.host}{request.url.path}")

    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", no_real_network):
        async with Client(srv) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            report["listed"] = len(tools)
            for name in sorted(tools):
                if name in SKIP:
                    continue
                calls.clear()
                res = await c.call_tool(name, ARGS.get(name, {}))
                report["called"] += 1
                env = as_envelope(res)
                if res.is_error or not isinstance(env, dict) or env.get("ok") is not True:
                    report["not_ok"].append(name)  # failures are the never-raise / outage tests' job
                    continue
                report["ok"] += 1
                if not calls:
                    report["no_network"].add(name)
                problems = shape_problems(env, bool(calls))
                if problems:
                    report["problems"][name] = "; ".join(problems)
    return report


async def test_successful_answers_have_the_envelope_shape(tmp_path):
    calls: list[str] = []
    srv, _ = build_server(_runtime(tmp_path, calls))
    report = await check_every_tool(srv, calls)

    assert report["listed"] >= len(EXPECTED_TOOLS)
    assert report["called"] == report["listed"] - len(SKIP)
    # 85 of 109 on 2026-11-06; the rest have no fake provider (Pendle, DefiLlama, GoPlus, DexScreener), no key
    # (Coinalyze) or need history in the store.
    assert report["ok"] >= 80, f"too few tools answered ok=true on the fakes; ok=false: {report['not_ok']}"
    new = {k: v for k, v in report["problems"].items() if k not in KNOWN}
    assert new == {}, "fix these tools (Builder.add / guard / warn), or list them in KNOWN and docs/BACKLOG.md"
    assert set(KNOWN) - set(report["problems"]) == set(), "these tools are fixed: remove them from KNOWN"
    assert report["no_network"] == NO_NETWORK, "update NO_NETWORK: the tools that answer ok=true without a provider call"


async def test_the_check_catches_a_tool_without_evidence(tmp_path):
    """Self-check: a tool that fetches but drops the evidence is reported, so a green run above means something."""
    calls: list[str] = []
    rt = _runtime(tmp_path, calls)
    srv, _ = build_server(rt)

    @srv.tool(name="zz_no_evidence_probe")
    async def zz_no_evidence_probe() -> dict:
        fetched = await rt.hyperliquid.asset_contexts()  # data used, evidence dropped (no Builder.add)
        return Builder().build({"rows": len(fetched.data)})

    report = await check_every_tool(srv, calls)
    assert "zz_no_evidence_probe" not in report["no_network"]
    assert "`evidence` is empty" in report["problems"]["zz_no_evidence_probe"]


def test_shape_problems_flags_a_broken_envelope():
    good = Builder().build({"x": 1})
    assert shape_problems(good, made_calls=False) == []
    assert shape_problems({"ok": True, "data": 1}, made_calls=False) == ["no `evidence`", "no `missing`", "no `warnings`", "no `as_of`"]
    bad = {**good, "evidence": [{"provider": "deribit", "fetched_at": "2026-11-06T10:00:00"}], "missing": [{"field": "x"}]}
    problems = shape_problems(bad, made_calls=True)
    assert any("no fetch time" in p for p in problems) and any("lacks field/provider/reason" in p for p in problems)


def test_lists_name_registered_tools_only():
    assert NO_NETWORK <= EXPECTED_TOOLS and set(KNOWN) <= EXPECTED_TOOLS
    assert NO_NETWORK.isdisjoint(KNOWN)
