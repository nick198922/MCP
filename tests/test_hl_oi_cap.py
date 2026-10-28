"""Hyperliquid OI-cap snapshot (IDEA-20261028-hl-oi-cap-state-forward, step 0): provider parsing, the pure row builder
(analytics/hl_oi_cap.py), the table hl_oi_cap_snapshot and the collector job `hl_oi_cap`."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from conftest import State, make_transports
from tradedesk.analytics.hl_oi_cap import hour_ts, snapshot_rows
from tradedesk.collector.hl_oi_cap import collect_hl_oi_cap
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.features import allowed
from tradedesk.providers.hyperliquid import parse_asset_contexts, parse_oi_cap
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

FIXTURE = Path(__file__).parent / "fixtures" / "hl_oi_cap_live_2026-10-28.json"
SAMPLE = json.loads(FIXTURE.read_text())  # live api.hyperliquid.xyz 2026-10-28 10:35 UTC, trimmed (see `_source`)
NOW = datetime(2026, 10, 28, 10, 35, 27, tzinfo=UTC)
HOUR = int(datetime(2026, 10, 28, 10, tzinfo=UTC).timestamp())
LISTED = ["ATOM", "BTC", "ETH", "kPEPE"]  # the listed perps of the sample, sorted; MATIC, FTM, JELLY are delisted
# Live, every capped ticker was a delisted perp; a listed one is added to cover at_cap = 1 (synthetic on the live sample).
CAPPED = ["FTM", "kPEPE"]


# ----------------------------------------------------------------------------- pure functions


def test_hour_ts_is_the_start_of_the_utc_hour():
    assert hour_ts(NOW) == HOUR == 1793181600
    assert hour_ts(datetime(2026, 10, 28, 10, tzinfo=UTC)) == HOUR
    assert hour_ts(datetime(2026, 10, 28, 10, 59, 59, 999_000, tzinfo=UTC)) == HOUR
    assert hour_ts(datetime(2026, 10, 28, 11, tzinfo=UTC)) == HOUR + 3600


def test_parse_oi_cap_accepts_a_list_of_tickers_only():
    assert parse_oi_cap(SAMPLE["perps_at_oi_cap"]) == ["CANTO", "FTM", "JELLY", "LOOM", "RLB", "VINE", "ZEREBRO"]
    assert parse_oi_cap([]) == []  # no perp at its cap is a valid answer
    for bad in ({"universe": []}, None, "FTM", ["FTM", 3]):
        with pytest.raises(ProviderError) as e:
            parse_oi_cap(bad)
        assert e.value.reason == "backend_error"


def test_asset_contexts_keep_delisted_only_on_request():
    raw = SAMPLE["meta_and_asset_ctxs"]
    default = parse_asset_contexts(raw)
    assert sorted(r["coin"] for r in default) == LISTED and all("delisted" not in r for r in default)  # unchanged default
    full = {r["coin"]: r["delisted"] for r in parse_asset_contexts(raw, include_delisted=True)}
    assert full == {"BTC": False, "ETH": False, "ATOM": False, "MATIC": True, "kPEPE": False, "FTM": True, "JELLY": True}


def test_snapshot_rows_on_the_live_sample():
    ctx = parse_asset_contexts(SAMPLE["meta_and_asset_ctxs"], include_delisted=True)
    rows = snapshot_rows(SAMPLE["perps_at_oi_cap"], ctx)
    by = {r["coin"]: r for r in rows}
    # delisted perps get no row, capped (FTM, JELLY) or not (MATIC)
    assert not {"MATIC", "FTM", "JELLY"} & by.keys()
    assert [c for c in LISTED if by[c]["at_cap"]] == []  # live: no listed perp at its cap
    # capped tickers outside the trimmed contexts keep their state with NULL market fields
    absent = ["CANTO", "LOOM", "RLB", "VINE", "ZEREBRO"]
    assert all(by[c]["at_cap"] == 1 and by[c]["oi_usd"] is None and by[c]["mark"] is None for c in absent)
    assert [r["coin"] for r in rows] == sorted(LISTED + absent)
    btc = by["BTC"]
    assert btc["oi_usd"] == pytest.approx(42465.64556 * 81520.1)  # OI in coins x mark, USD
    assert (btc["funding"], btc["mark"], btc["oracle"]) == (0.0000125, 81520.1, 81544.4)  # hourly decimal funding
    assert btc["vol24h"] == pytest.approx(1847395436.33235)
    assert by["ATOM"]["funding"] == pytest.approx(-0.0000565426)


def test_snapshot_rows_capped_listed_perp_and_duplicates():
    ctx = parse_asset_contexts(SAMPLE["meta_and_asset_ctxs"], include_delisted=True)
    rows = snapshot_rows(CAPPED + ["kPEPE", ""], ctx + ctx[:1])  # duplicate ticker, empty ticker, duplicate context
    assert [(r["coin"], r["at_cap"]) for r in rows] == [("ATOM", 0), ("BTC", 0), ("ETH", 0), ("kPEPE", 1)]  # exact case kept
    pepe = rows[-1]
    assert pepe["oi_usd"] == pytest.approx(10192768474.0 * 0.004989) and pepe["funding"] == pytest.approx(1.25e-5)
    assert snapshot_rows([], []) == []


# ----------------------------------------------------------------------------- collector


def _rt(tmp_path, *, opts: dict | None = None, **kw) -> tuple[Runtime, list]:
    """Fake Hyperliquid /info serving the sample. ``opts`` (read on every request): "cap" = the cap answer (default
    ``CAPPED``); "down" = a set of request types answering 500."""
    calls: list = []
    opts = opts if opts is not None else {}
    opts.setdefault("down", set())
    opts.setdefault("cap", CAPPED)

    def hl(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        calls.append(t)
        if t in opts["down"]:
            return httpx.Response(500, text="down")
        if t == "perpsAtOpenInterestCap":
            return httpx.Response(200, json=opts["cap"])
        if t == "metaAndAssetCtxs":
            return httpx.Response(200, json=SAMPLE["meta_and_asset_ctxs"])
        return httpx.Response(422, text="unknown type")

    tr = make_transports(State())
    tr["hyperliquid"] = httpx.MockTransport(hl)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite"), **kw), transports=tr)
    rt.now = lambda: NOW
    rt.hyperliquid.http._backoff = 0.0
    return rt, calls


async def test_job_stores_one_snapshot_per_hour(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_hl_oi_cap(rt) == 4
        assert sorted(calls) == ["metaAndAssetCtxs", "perpsAtOpenInterestCap"]
        rows = await rt.store.hl_oi_cap_rows()
        assert [r["coin"] for r in rows] == LISTED
        assert {r["ts"] for r in rows} == {HOUR} and {r["fetched_ts"] for r in rows} == {int(NOW.timestamp())}
        pepe = rows[-1]
        assert pepe["at_cap"] == 1 and sum(r["at_cap"] for r in rows) == 1
        assert pepe["oi_usd"] == pytest.approx(10192768474.0 * 0.004989) and pepe["funding"] == pytest.approx(1.25e-5)
        assert (pepe["mark"], pepe["oracle"]) == (0.004989, 0.004988) and pepe["vol24h"] == pytest.approx(29579921.43)
        # a later tick in the same hour: no request, nothing written
        rt.now = lambda: NOW + timedelta(minutes=20)
        assert await collect_hl_oi_cap(rt) == 0 and len(calls) == 2
        # the next hour: a new snapshot
        rt.now = lambda: NOW + timedelta(minutes=40)
        assert await collect_hl_oi_cap(rt) == 4 and len(calls) == 4
        assert [r["ts"] for r in await rt.store.hl_oi_cap_rows(coin="BTC")] == [HOUR, HOUR + 3600]
    finally:
        await rt.aclose()


@pytest.mark.parametrize(
    ("opts", "field"),
    [
        ({"down": {"perpsAtOpenInterestCap"}}, "hl_perps_at_oi_cap: backend_error"),
        ({"down": {"metaAndAssetCtxs"}}, "hl_asset_contexts: backend_error"),
        ({"cap": {"error": "reshaped"}}, "hl_perps_at_oi_cap: backend_error unexpected perpsAtOpenInterestCap shape"),
    ],
)
async def test_either_request_failing_records_nothing_and_never_raises(tmp_path, caplog, opts, field):
    rt, _ = _rt(tmp_path, opts=opts)
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_hl_oi_cap(rt) == 0
        assert await rt.store.hl_oi_cap_rows() == []
        assert f"hl_oi_cap: hour {HOUR} not recorded" in caplog.text and field in caplog.text
        opts["down"], opts["cap"] = set(), CAPPED
        rt.now = lambda: NOW + timedelta(minutes=10)  # recovers on the next tick of the same hour
        assert await collect_hl_oi_cap(rt) == 4
    finally:
        await rt.aclose()


async def test_empty_universe_records_nothing(tmp_path, caplog):
    rt, _ = _rt(tmp_path)
    try:
        original = rt.hyperliquid.asset_contexts

        async def empty(include_delisted: bool = False):
            f = await original(include_delisted)
            return type(f)([], f.evidence)

        rt.hyperliquid.asset_contexts = empty
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_hl_oi_cap(rt) == 0
        assert "hl_asset_contexts: data_unavailable" in caplog.text and await rt.store.hl_oi_cap_rows() == []
    finally:
        await rt.aclose()


async def test_no_store_never_raises(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        rt.store = None
        assert await collect_hl_oi_cap(rt) == 0 and calls == []
    finally:
        await rt.aclose()


async def test_insert_is_idempotent_for_the_same_hour(tmp_path):
    s = Store(tmp_path / "x.sqlite")
    await s.init()
    try:
        row = {"coin": "kPEPE", "at_cap": 1, "oi_usd": 5.0e7, "funding": 1.25e-5, "mark": 0.005, "oracle": 0.005, "vol24h": 3e7}
        assert await s.insert_hl_oi_cap_snapshot(HOUR, HOUR + 60, [row, {"at_cap": 0}, {"coin": "BTC"}]) == 1  # no coin / state: dropped
        assert await s.insert_hl_oi_cap_snapshot(HOUR, HOUR + 900, [dict(row, at_cap=0, mark=9.9)]) == 0  # first one kept
        assert await s.hl_oi_cap_has_hour(HOUR) and not await s.hl_oi_cap_has_hour(HOUR + 3600)
        assert await s.hl_oi_cap_rows() == [{"ts": HOUR, **row, "fetched_ts": HOUR + 60}]
        assert await s.insert_hl_oi_cap_snapshot(HOUR, HOUR + 900, [dict(row, coin="BTC", at_cap=0)]) == 1  # a new coin
    finally:
        await s.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    s = Settings()
    assert s.collect_hl_oi_cap_enabled is False and s.collect_hl_oi_cap_seconds == 600
    assert allowed("TRADEDESK_COLLECT_HL_OI_CAP_ENABLED") and allowed("TRADEDESK_COLLECT_HL_OI_CAP_SECONDS")
    monkeypatch.setenv("TRADEDESK_COLLECT_HL_OI_CAP_ENABLED", "1")
    assert Settings.from_env().collect_hl_oi_cap_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_hl_oi_cap_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "hl_oi_cap" not in names(off) and "hl_oi_cap" in names(on)
    job = next(j for j in build_default_scheduler(on).jobs if j.name == "hl_oi_cap")
    assert job.period_seconds == 600 and job.fn is collect_hl_oi_cap


async def test_migration_31_adds_the_table_to_a_v30_store(tmp_path):
    path = tmp_path / "v30.sqlite"
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= 30:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 30")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 31
        assert await s.insert_hl_oi_cap_snapshot(HOUR, HOUR + 1, [{"coin": "BTC", "at_cap": 0, "mark": 81520.1}]) == 1
        got = await s.hl_oi_cap_rows()
        assert got == [{"ts": HOUR, "coin": "BTC", "at_cap": 0, "oi_usd": None, "funding": None, "mark": 81520.1, "oracle": None,
                        "vol24h": None, "fetched_ts": HOUR + 1}]  # fmt: skip
    finally:
        await s.aclose()
