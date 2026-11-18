"""End-to-end check of the Envelope shape of successful tool answers (CLAUDE.md invariants 1-2, core/envelope.py).

Complements tests/test_tools_never_raise.py and tests/test_tools_provider_outage.py, which check that failing providers
still give an envelope. Here every registered tool is called through a real MCP client against the happy-path fakes of
tests/conftest.py (Deribit / Hyperliquid / Binance / Bybit / Polymarket / CoinGecko / ... transports and FakeEx), plus
Pendle, DefiLlama, GoPlus, DexScreener, Binance daily klines and Coinalyze (a test-only dummy key) served from the saved
samples in tests/fixtures, the Polymarket events and books of tests/test_pm_gap.py, and a store seeded with funding /
ATM-term / OHLCV history, an event, a position and a paper carry pair; providers without a fake answer 404, and nothing
reaches the real network. Arguments and SKIP come from tests/test_tools_never_raise.py. For every answer with ok=true it
checks:
- the keys and types of the Envelope: ok (bool), data, evidence / missing / warnings (lists), as_of (ISO time with zone);
- every `evidence` item names its provider and its fetch time (ISO with zone); every `missing` item has field,
  provider and reason; every warning is a string;
- a tool that made a provider call has at least one `evidence` item (otherwise the numbers lose their source);
- ok=true with a non-empty `missing` comes with at least one warning;
- a tool that answers ok=true with no provider call at all (calculators, the local store) is listed in NO_NETWORK.
Answers with ok=false on these fakes (no fake for the provider, an empty store) are not checked here; they are counted
and named in the assertion message when too many tools fall out.
KNOWN: violations found and not fixed yet (each one also in docs/BACKLOG.md); a stale entry fails the test.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import patch

import httpx
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from test_coinalyze import SAMPLE as LIQ_SAMPLE
from test_coinalyze_history import SAMPLES as COINALYZE_SAMPLES
from test_contract_risk import DEX_PAIRS as SHIB_DEX_PAIRS
from test_contract_risk import SHIB, SHIB_BODY
from test_cycle_regime import FX as CYCLE_FX
from test_cycle_regime import serve_daily
from test_onchain import DEX as FART_DEX
from test_onchain import FART, LLAMA
from test_pendle import PENDLE, SUSDE_ETH
from test_pm_gap import BOOKS as PM_BOOKS
from test_pm_gap import _events as pm_events
from test_server import EXPECTED_TOOLS
from test_stablecoin_supply import default_handler as stablecoins_handler
from test_tools_never_raise import ARGS as NEVER_RAISE_ARGS
from test_tools_never_raise import SKIP
from test_tools_provider_outage import as_envelope
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

EXPIRY = "2026-11-27T08:00:00+00:00"  # a Deribit expiry of the conftest option book, seeded into atm_term below
COINALYZE_TEST_KEY = "test-dummy-key"  # not a real key: the Coinalyze transport below is a fake

# events_add needs a time (ts_ms or date) to succeed; the outage tests only need it to reach the store. The rest point
# the on-chain / Pendle / history / Polymarket tools at what the saved samples and the seeded store hold (the outage tests
# only need any valid argument). The seeded store has position 1, so positions_close takes the row positions_add creates
# (id 2); journal_close takes journal row 1, which journal_add creates (it was row 2 while carry_paper_close, removed
# 2026-11-18, closed the seeded carry pair into the journal first).
ARGS: dict[str, dict] = {
    **NEVER_RAISE_ARGS,
    "events_add": {**NEVER_RAISE_ARGS["events_add"], "date": "2026-11-05"},
    "chain_metrics": {"chain": "Solana"},
    "contract_risk": {"chain": "ethereum", "address": SHIB},
    "dex_pairs": {"addresses": [FART]},
    "iv_rank": {"expiry": EXPIRY},
    "journal_close": {**NEVER_RAISE_ARGS["journal_close"], "id": 1},
    "pendle_market": {"market": SUSDE_ETH},
    "pendle_yield_curve": {"asset": "sUSDe"},
    "polymarket_options_gap": {"max_days": 30, "min_net_edge": -1},
    "positions_close": {"id": 2},
    "skew_percentile": {"expiry": EXPIRY},
}

# Tools that answered ok=false on the conftest fakes (item 173) and answer ok=true here thanks to the fakes and the
# seeded store below (items 176 and 181); the test fails if one of them falls back to ok=false (the shape stops being checked).
EXTRA_OK: set[str] = {
    # providers faked from saved samples: Pendle (2026-10-18), DefiLlama (2026-10-17, stablecoins 2026-11-05 synthetic),
    # GoPlus (2026-10-23) + DexScreener, Binance daily klines (2026-10-28)
    "pendle_expiry_calendar",
    "pendle_fixed_yields",
    "pendle_market",
    "pendle_yield_curve",
    "chain_metrics",
    "stablecoin_supply",
    "contract_risk",
    "dex_pairs",
    "cycle_regime",
    # the seeded store
    "funding_zscore",
    "iv_rank",
    "market_regime",
    "ohlcv_history",
    "range_detect",
    "skew_percentile",
    # item 181: Coinalyze with a test-only dummy key (samples 2026-10-17 / 2026-10-18), Gamma events + CLOB books of
    # tests/test_pm_gap.py, and the store seeded with an event, a position, a paper carry pair and 4h bars with a sweep
    "liquidations_history",
    "polymarket_options_gap",
    "event_study",
    "positions_risk",
}

ENVELOPE_TYPES: dict[str, type] = {"ok": bool, "evidence": list, "missing": list, "warnings": list, "as_of": str}

# Tools that answer ok=true without any provider call, so an empty `evidence` is right for them.
NO_NETWORK: set[str] = {
    # pure calculators
    "odds_devig",
    "option_calc",
    "strategy_cash_and_carry",
    "strategy_funding_carry",
    "strategy_payoff",
    "strategy_scenario_matrix",
    "touch_probability",
    # the local store (SQLite), the bundled event seed and the in-process health registry
    "alerts_recent",
    "assistant_paper_status",
    "events_add",
    "events_list",
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
    "rules_test",
    "system_health",
    # history in the local store (seeded by _seed_store)
    "event_study",
    "funding_zscore",
    "iv_rank",
    "market_regime",
    "ohlcv_history",
    "positions_risk",
    "range_detect",
    "skew_percentile",
}

# Violations found and not fixed yet (each also in docs/BACKLOG.md). Format: {"tool": "what is wrong"}.
# The four perps_* partial answers (BACKLOG 6.47) were fixed on 2026-11-06 (item 174).
KNOWN: dict[str, str] = {}


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


DAY_MS = 86_400_000
# The cycle_regime sample was captured on 2026-10-28; its daily rows are moved back so they end where NOW's day starts.
CYCLE_SHIFT_MS = CYCLE_FX["end_ms"] - int(NOW.timestamp() * 1000) // DAY_MS * DAY_MS


class _DailyKlines(httpx.AsyncBaseTransport):
    """BTCUSDT daily klines of the cycle_regime sample (moved to NOW); every other request goes to the conftest fake."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return serve_daily(request, CYCLE_SHIFT_MS) or await self._inner.handle_async_request(request)


def _pendle(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/core/v1/markets/all":
        return httpx.Response(200, json=PENDLE)
    return _not_mocked(req)


def _defillama(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    for prefix, key in (("/v2/historicalChainTvl/", "tvl"), ("/overview/dexs/", "dexs"), ("/overview/fees/", "fees")):
        if p.startswith(prefix):
            return httpx.Response(200, json=LLAMA[key])
    return _not_mocked(req)


def _dexscreener(req: httpx.Request) -> httpx.Response:
    if not req.url.path.startswith("/latest/dex/tokens/"):
        return _not_mocked(req)
    return httpx.Response(200, json=SHIB_DEX_PAIRS if SHIB in req.url.path.lower() else FART_DEX)


# Coinalyze rows by endpoint: liquidations from the 2026-10-17 sample, OI / funding / long-short from the 2026-10-18 ones.
COINALYZE_HISTORY = {**COINALYZE_SAMPLES, "/v1/liquidation-history": LIQ_SAMPLE[0]["history"]}


def _coinalyze(req: httpx.Request) -> httpx.Response:
    rows = COINALYZE_HISTORY.get(req.url.path)
    if rows is None:
        return _not_mocked(req)
    return httpx.Response(200, json=[{"symbol": req.url.params["symbols"], "history": rows}])


class _PolymarketPmGap(httpx.AsyncBaseTransport):
    """Gamma's /events/keyset and CLOB's /books as tests/test_pm_gap.py serves them; the rest goes to the conftest fake."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events/keyset":
            return httpx.Response(200, json={"events": pm_events(), "next_cursor": None})
        if request.url.path == "/books":
            wanted = [x["token_id"] for x in json.loads(request.content)]
            return httpx.Response(200, json=[PM_BOOKS[t] for t in wanted if t in PM_BOOKS])
        return await self._inner.handle_async_request(request)


def _extra_fakes(fakes: dict) -> dict:
    """The conftest fakes plus the providers item 173 had no fake for, all from samples in tests/fixtures."""
    return {
        **fakes,
        "pendle": httpx.MockTransport(_pendle),
        "defillama": httpx.MockTransport(_defillama),
        "defillama:stablecoins": httpx.MockTransport(stablecoins_handler),
        "goplus": httpx.MockTransport(lambda req: httpx.Response(200, json=SHIB_BODY)),
        "dexscreener": httpx.MockTransport(_dexscreener),
        "binance_spot": _DailyKlines(fakes["binance_spot"]),
        "binance_futures": _DailyKlines(fakes["binance_futures"]),
        "coinalyze": httpx.MockTransport(_coinalyze),
        "polymarket:gamma": _PolymarketPmGap(fakes["polymarket:gamma"]),
        "polymarket:clob": _PolymarketPmGap(fakes["polymarket:clob"]),
    }


def _sweep_bars(end_ts: int) -> list[list[float]]:
    """54 4h BTC bars inside 81 400 - 82 600, then at bar 50 a wick to 83 500 that closes back at 82 550 (below the swing
    high, above the range mid), i.e. one high-side sweep inside the range for range_setup."""
    out = []
    for i in range(54):
        o, h, lo, c = (82_500.0, 82_600.0, 81_400.0, 81_500.0) if i % 2 == 0 else (81_500.0, 82_600.0, 81_400.0, 82_500.0)
        if i == 50:
            o, h, lo, c = 82_500.0, 83_500.0, 82_450.0, 82_550.0
        out.append([(end_ts - (54 - i) * 4 * 3600) * 1000, o, h, lo, c, 10.0])
    return out


async def _seed_store(rt: Runtime) -> None:
    """History the store-backed tools read (as tests/test_history.py and tests/test_regime.py seed it): hourly funding
    and ATM-term points before NOW, and 200 hourly BTC bars that swing inside a 4 % band. Item 181 adds an FOMC event
    inside those bars (event_study), an open option position with greeks (positions_risk), an open paper carry pair
    (read by paper_carry_status(strategy='manual'); carry_paper_close, which closed it, was removed 2026-11-18) and 4h BTC
    bars with a sweep (range_detect, view='setup')."""
    await rt.store.init()
    now_ts = int(NOW.timestamp())
    await rt.store.event_insert(ts=now_ts - 100 * 3600, kind="fomc", label="FOMC (envelope-shape seed)", impact="high")
    await rt.store.position_insert(
        opened_ts=now_ts - 86_400, instrument="BTC-27NOV26-90000-C", kind="option", side="long", size=1.0, entry=1400.0,
        mark=1500.0, delta=0.3, gamma=0.00002, vega=95.0, theta=-40.0, tags=["seed"],
    )  # fmt: skip
    await rt.store.carry_paper_insert(
        opened_ts=now_ts - 86_400, base="BTC", symbol="BTC/USDT:USDT", long_venue="binanceusdm", short_venue="bybit", qty=0.01,
        notional_usd=1000.0, long_entry=82750.0, short_entry=82750.0, leverage=2.0, stop_move=0.5, lookback_days=7.0, exit_apr=0.0,
        slippage=0.0005,
    )  # fmt: skip
    await rt.store.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "4h", _sweep_bars(now_ts // 14_400 * 14_400))
    for i in range(20):
        ts = now_ts - (19 - i) * 3600
        await rt.store.insert_funding(
            ts=ts,
            rows=[
                {"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0001, "interval_hours": 8.0, "annualized": 0.08 + 0.005 * i}
            ],
        )
        await rt.store.insert_atm_term(
            ts=ts,
            currency="BTC",
            rows=[
                {"expiry": EXPIRY, "days": 48.0, "forward": 83000.0, "atm_iv": 0.40 + 0.005 * i, "rr25": -0.05 + 0.002 * i, "fly25": 0.009}
            ],
        )
    start_ms = (now_ts - 200 * 3600) * 1000
    closes = [82000.0 + 1500.0 * ((i % 24) - 12) / 12 for i in range(200)]
    bars = [[start_ms + i * 3_600_000, c, c * 1.002, c * 0.998, c, 10.0] for i, c in enumerate(closes)]
    await rt.store.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "1h", bars)


def _runtime(tmp_path, calls: list[str]) -> Runtime:
    fakes = _extra_fakes(make_transports(State()))
    rt = Runtime(
        Settings(
            http_retries=0,
            http_retry_backoff=0.0,
            cache_scale=0.0,
            db_path=str(tmp_path / "envelope_shape.sqlite"),
            coinalyze_api_key=COINALYZE_TEST_KEY,
        ),
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
    rt = _runtime(tmp_path, calls)
    await _seed_store(rt)
    srv, _ = build_server(rt)
    report = await check_every_tool(srv, calls)

    assert report["listed"] >= len(EXPECTED_TOOLS)
    assert report["called"] == report["listed"] - len(SKIP)
    assert EXTRA_OK.isdisjoint(report["not_ok"]), f"no longer ok=true on the fakes: {sorted(EXTRA_OK & set(report['not_ok']))}"
    # 109 of 109 on 2026-11-06 (85 before item 176, 100 before item 181); 105 tools since the strategy presets merged,
    # 99 of 99 since the coinalyze_* history tools merged into perps_* (2026-11-18), 97 since the range_* merge,
    # 91 of 91 since six tools were removed (2026-11-18, docs/changes/2026-11-18-mcp-remove-six.md).
    # relative to the tool count, so removing a tool does not break the guard
    assert report["ok"] >= len(EXPECTED_TOOLS) - 2, f"too few tools answered ok=true on the fakes; ok=false: {report['not_ok']}"
    new = {k: v for k, v in report["problems"].items() if k not in KNOWN}
    assert new == {}, "fix these tools (Builder.add / guard / warn), or list them in KNOWN and docs/BACKLOG.md"
    assert set(KNOWN) - set(report["problems"]) == set(), "these tools are fixed: remove them from KNOWN"
    assert report["no_network"] == NO_NETWORK, "update NO_NETWORK: the tools that answer ok=true without a provider call"


async def test_coinalyze_source_answers_have_the_envelope_shape(tmp_path):
    """source='coinalyze' of the three perps_* history tools (the former coinalyze_* tools in EXTRA_OK, merged
    2026-11-18): the sweep above calls the default source only."""
    calls: list[str] = []
    srv, _ = build_server(_runtime(tmp_path, calls))
    async with Client(srv) as c:
        for name in ("perps_funding_history", "perps_long_short_ratio", "perps_oi_history"):
            calls.clear()
            env = as_envelope(await c.call_tool(name, {"source": "coinalyze", "base": "BTC"}))
            assert env["ok"] is True, (name, env)
            assert calls and all("coinalyze" in x for x in calls), (name, calls)
            assert shape_problems(env, made_calls=True) == [], name


async def test_range_detect_setup_and_scan_views_have_the_envelope_shape(tmp_path):
    """range_detect's view='setup' (store, the 4h sweep bars of _seed_store) and view='scan' (live FakeEx), the former
    range_setup (in EXTRA_OK / NO_NETWORK) and range_setup_scan, merged 2026-11-18: the sweep above calls view='ranges'."""
    calls: list[str] = []
    rt = _runtime(tmp_path, calls)
    await _seed_store(rt)
    srv, _ = build_server(rt)
    base = {"venue": "binanceusdm"}
    async with Client(srv) as c:
        for args, network in (
            ({"symbol": "BTC/USDT:USDT", "view": "setup", "source": "store"}, False),
            ({"symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"], "view": "scan"}, True),
        ):
            calls.clear()
            env = as_envelope(await c.call_tool("range_detect", {**base, **args}))
            assert env["ok"] is True, (args, env)
            assert bool(calls) is network, (args, calls)
            assert shape_problems(env, made_calls=network) == [], args


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
