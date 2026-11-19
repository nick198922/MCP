"""yield_pools / yield_pool_history (DefiLlama yields.llama.fi) on a SYNTHETIC sample of the documented shape behind
httpx.MockTransport (tests/fixtures/defillama_yields_sample_2026-11-19.json, not a real capture: see its `_note`; the
field names were checked live on 2026-11-19).
Numbers are round so every expected value can be checked by hand; APYs arrive in percent and leave as decimals."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from test_server import call
from tradedesk.analytics.yields import filter_pools, normalise_pools, pool_history, pool_row, valid_pool_id
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = json.loads((Path(__file__).parent / "fixtures" / "defillama_yields_sample_2026-11-19.json").read_text())
POOL = FIX["chart_pool"]  # the aave-v3 USDC pool of the sample
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)  # latest chart point is 2026-11-18 23:00 UTC
SRC = "defillama:yields"


def serve(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/pools":
        return httpx.Response(200, json=FIX["pools"])
    if req.url.path == f"/chart/{POOL}":
        return httpx.Response(200, json=FIX["chart"])
    return httpx.Response(404, text="not mocked")


def _down(req: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


def make_server(handler=serve, cache_scale: float = 0.0):
    # The production bucket (1 req/s) would only pace the fakes in real time.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(
            Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=cache_scale), transports={SRC: httpx.MockTransport(handler)}
        )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


# ---------------------------------------------------------------- yield_pools


async def test_yield_pools_happy():
    env = await call(make_server(), "yield_pools")
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    # 7 objects in the sample: two lack a TVL or an id; the non-object row is dropped by the provider
    assert (d["pools_total"], d["pools_skipped"]) == (5, 2)
    # default min_tvl_usd 1M drops the $400k pool; sorted by TVL, largest first
    assert d["pools_matched"] == 4 and [p["project"] for p in d["pools"]] == ["lido", "aave-v3", "uniswap-v3", "newfarm"]
    aave = d["pools"][1]
    assert aave["pool"] == POOL and aave["chain"] == "Ethereum" and aave["tvl_usd"] == 3.5e9
    # percent -> decimals
    assert aave["apy"] == pytest.approx(0.046) and aave["apy_base"] == pytest.approx(0.041) and aave["apy_reward"] == pytest.approx(0.005)
    assert aave["apy_change_7d"] == pytest.approx(-0.004) and aave["apy_mean_30d"] == pytest.approx(0.043)
    assert aave["stablecoin"] is True and aave["history_days"] == 900
    assert aave["underlying_tokens"] == ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]
    uni = d["pools"][2]
    assert uni["il_risk"] == "yes" and uni["il_7d"] == pytest.approx(-0.0035) and uni["volume_usd_1d"] == 1.2e8
    assert d["pools"][0]["apy_reward"] is None and d["pools"][0]["reward_tokens"] == []
    farm = d["pools"][3]
    assert farm["outlier"] is True and farm["apy"] == pytest.approx(9.53) and farm["pool_meta"] == "Look-alike farm"
    assert d["outliers_returned"] == 1
    assert len(env["evidence"]) == 1 and env["evidence"][0]["provider"] == SRC
    w = " ".join(env["warnings"])
    assert "2 malformed pool entries" in w and "outlier" in w and "not a promise" in w and "contract_risk" in w


async def test_yield_pools_filters_and_sorting():
    srv = make_server()
    env = await call(srv, "yield_pools", stablecoin_only=True, min_tvl_usd=0)
    assert [p["pool"][-1] for p in env["data"]["pools"]] == ["2", "4", "5"]
    env = await call(srv, "yield_pools", chain="arb", min_tvl_usd=0)  # alias -> Arbitrum
    assert env["data"]["filters"]["chain"] == "Arbitrum" and [p["project"] for p in env["data"]["pools"]] == ["uniswap-v3", "aave-v3"]
    env = await call(srv, "yield_pools", project="AAVE-V3", min_tvl_usd=0, limit=1)
    assert env["data"]["pools_matched"] == 2 and env["data"]["pools_returned"] == 1 and env["data"]["pools"][0]["chain"] == "Ethereum"
    env = await call(srv, "yield_pools", sort_by="apy")
    assert [p["project"] for p in env["data"]["pools"]] == ["newfarm", "uniswap-v3", "aave-v3", "lido"]
    env = await call(srv, "yield_pools", sort_by="newest")
    assert [p["history_days"] for p in env["data"]["pools"]] == [3, 700, 900, 1200]
    assert any("history_days counts" in w for w in env["warnings"])
    env = await call(srv, "yield_pools", chain="Solana")
    assert env["ok"] is True and env["data"]["pools"] == [] and any("No pool matches" in w for w in env["warnings"])


async def test_yield_pools_provider_failure():
    env = await call(make_server(_down), "yield_pools")
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["provider"], m["reason"]) for m in env["missing"]] == [("pools", SRC, "backend_error")]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ([{"pool": "x"}], "backend_error"),  # a bare list instead of {status, data}
        ({"status": "error", "data": []}, "backend_error"),
        ({"status": "success", "data": [{"pool": None}, "x"]}, "data_unavailable"),
    ],
)
async def test_yield_pools_unexpected_shapes(body, reason):
    env = await call(make_server(lambda req: httpx.Response(200, json=body)), "yield_pools")
    assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("pools", reason)]


async def test_yield_pools_rejects_a_non_finite_tvl_floor():
    env = await call(make_server(), "yield_pools", min_tvl_usd="inf")
    assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_input"


async def test_pool_list_is_cached_slim_in_the_provider():
    hits = []

    def counting(req):
        hits.append(req.url.path)
        return serve(req)

    srv = make_server(counting, cache_scale=1.0)
    first = await call(srv, "yield_pools")
    second = await call(srv, "yield_pools", stablecoin_only=True)
    assert hits == ["/pools"]  # one download serves both calls
    assert first["evidence"][0]["cached"] is False and second["evidence"][0]["cached"] is True


async def test_pool_list_is_normalised_once_and_results_match_an_uncached_server():
    calls = []

    def counting(rows):
        calls.append(len(rows))
        return normalise_pools(rows)

    queries = [{}, {"stablecoin_only": True, "min_tvl_usd": 0}, {"chain": "arb", "min_tvl_usd": 0}, {"sort_by": "apy"},
               {"sort_by": "newest"}, {"project": "AAVE-V3", "min_tvl_usd": 0, "limit": 1}]  # fmt: skip
    with patch("tradedesk.providers.defillama.normalise_pools", counting):
        cached = make_server(cache_scale=1.0)
        got = [await call(cached, "yield_pools", **q) for q in queries]
    assert calls == [7]  # the 7 objects of the sample, parsed on the one download only
    fresh = [await call(make_server(), "yield_pools", **q) for q in queries]  # no cache: parsed per call
    assert [e["data"] for e in got] == [e["data"] for e in fresh]
    assert got[0]["data"]["pools_total"] == 5 and got[1]["data"]["pools_skipped"] == 2


# ---------------------------------------------------------------- yield_pool_history


async def test_yield_pool_history_happy():
    env = await call(make_server(), "yield_pool_history", pool=POOL.upper())  # ids are matched lower-case
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["pool"] == POOL and d["first_date"] == "2026-10-14" and d["points_total"] == 10
    assert d["latest"] == {"date": "2026-11-18", "tvl_usd": 3.5e9, "apy": pytest.approx(0.046), "apy_base": pytest.approx(0.041),
                           "apy_reward": pytest.approx(0.005), "il_7d": None}  # fmt: skip
    assert d["age_days"] == pytest.approx(0.54) and d["stale"] is False
    assert d["tvl_change_7d"] == pytest.approx(3.5 / 3.2 - 1, abs=1e-6)  # vs 2026-11-11
    assert d["tvl_change_30d"] == pytest.approx(3.5 / 2.8 - 1, abs=1e-6)  # vs 2026-10-19
    # the last 7 days up to the latest point: 4.2, 4.4, 4.4, 4.6, 4.6, 4.8 (11-17: the later point of the day), 4.6
    assert d["apy_mean_7d"] == pytest.approx(31.6 / 7 / 100)
    assert d["apy_mean_30d"] == pytest.approx(35.6 / 8 / 100)
    assert (d["apy_min"], d["apy_max"]) == (pytest.approx(0.04), pytest.approx(0.06))
    assert len(d["series"]) == 10 and d["series"][-2]["apy"] == pytest.approx(0.048)
    assert env["evidence"][0]["provider"] == SRC and env["evidence"][0]["args"] == {"pool": POOL}
    assert any("contract_risk" in w for w in env["warnings"])


async def test_yield_pool_history_window_and_stale():
    env = await call(make_server(), "yield_pool_history", pool=POOL, days=7)
    assert [p["date"] for p in env["data"]["series"]][0] == "2026-11-12" and len(env["data"]["series"]) == 7
    old = {"status": "success", "data": [{"timestamp": "2026-07-01T23:00:00.000Z", "tvlUsd": 100.0, "apy": 1.0}]}
    env = await call(make_server(lambda req: httpx.Response(200, json=old)), "yield_pool_history", pool=POOL)
    assert env["ok"] is True and env["data"]["stale"] is True and env["data"]["series"] == []
    assert env["data"]["tvl_change_7d"] is None
    assert any("stale" in w for w in env["warnings"]) and any("No points in the last 90 days" in w for w in env["warnings"])


async def test_yield_pool_history_provider_failure():
    env = await call(make_server(_down), "yield_pool_history", pool=POOL)
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["provider"], m["reason"]) for m in env["missing"]] == [("history", SRC, "backend_error")]


async def test_yield_pool_history_bad_id_makes_no_request():
    hits = []

    def counting(req):
        hits.append(req.url.path)
        return serve(req)

    for bad in ("../pools", "lido", POOL + "0", ""):
        env = await call(make_server(counting), "yield_pool_history", pool=bad)
        assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_argument"
    assert hits == []


async def test_yield_pool_history_empty_answer_is_data_unavailable():
    empty = {"status": "success", "data": []}
    env = await call(make_server(lambda req: httpx.Response(200, json=empty)), "yield_pool_history", pool=POOL)
    assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("history", "data_unavailable")]


# ---------------------------------------------------------------- pure helpers


def test_valid_pool_id():
    assert valid_pool_id(f"  {POOL.upper()} ") == POOL
    for bad in (None, 5, "", "lido", "../chart", POOL[:-1] + "g", POOL.replace("-", "")):
        assert valid_pool_id(bad) is None


def test_pool_row_rejects_and_cleans():
    assert pool_row("x") is None
    assert pool_row({"pool": "p", "chain": "Ethereum", "project": "a", "tvlUsd": -1}) is None
    assert pool_row({"pool": "p", "chain": "Ethereum", "project": "a", "tvlUsd": True}) is None
    row = pool_row({"pool": "p", "chain": "Ethereum", "project": "a", "tvlUsd": 10, "apy": 1e400, "stablecoin": "yes", "count": -3})
    assert row["apy"] is None and row["stablecoin"] is None and row["history_days"] is None
    many = pool_row({"pool": "p", "chain": "c", "project": "a", "tvlUsd": 1, "rewardTokens": [f"0x{i}" for i in range(30)] + [5]})
    assert len(many["reward_tokens"]) == 10


def test_filter_pools_unknown_sort_falls_back_to_tvl():
    norm = normalise_pools([r for r in FIX["pools"]["data"] if isinstance(r, dict)])
    assert (len(norm["pools"]), norm["skipped"]) == (5, 2)
    assert filter_pools(norm["pools"], sort_by="nope", min_tvl_usd=0)["sort_by"] == "tvl_usd"


def test_filter_pools_does_not_mutate_the_cached_rows():
    norm = normalise_pools([r for r in FIX["pools"]["data"] if isinstance(r, dict)])
    before = [dict(r) for r in norm["pools"]]
    out = filter_pools(norm["pools"], min_tvl_usd=0)["pools"]
    out[0]["tvl_usd"] = -1.0
    assert norm["pools"] == before


def test_pool_history_without_usable_points():
    assert pool_history([], NOW.timestamp()) is None
    assert pool_history([{"timestamp": "x", "apy": 1.0}, {"timestamp": "2026-11-01T00:00:00Z", "tvlUsd": -5}], NOW.timestamp()) is None


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
async def test_yield_pool_history_skips_a_point_outside_the_utc_date_range(stamp):
    # Both parse, but their UTC instant falls outside datetime's years 1..9999: the point is skipped, not fatal.
    body = {"status": "success", "data": [{"timestamp": "2026-11-18T23:00:00.000Z", "tvlUsd": 100.0, "apy": 5.0},
                                          {"timestamp": stamp, "tvlUsd": 200.0, "apy": 7.0}]}  # fmt: skip
    env = await call(make_server(lambda req: httpx.Response(200, json=body)), "yield_pool_history", pool=POOL)
    assert env["ok"] is True and env["missing"] == []
    assert env["data"]["points_total"] == 1 and env["data"]["latest"]["date"] == "2026-11-18"
    assert env["data"]["latest"]["apy"] == pytest.approx(0.05)


def test_pool_history_window_can_hold_days_plus_one_dates():
    # The newest point is today's intraday one: days=3 counted back from now spans 4 UTC dates (as the description says).
    rows = [{"timestamp": f"2026-11-{d}T23:00:00.000Z", "tvlUsd": 1.0, "apy": 1.0} for d in (15, 16, 17, 18)]
    rows.append({"timestamp": "2026-11-19T11:00:00.000Z", "tvlUsd": 1.0, "apy": 2.0})
    h = pool_history(rows, NOW.timestamp(), days=3)
    assert [p["date"] for p in h["series"]] == ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    # the 7-day mean counts back from the latest point (11-19 11:00), so 11-15 23:00 is still inside
    assert h["apy_mean_7d"] == pytest.approx(6.0 / 5 / 100)
