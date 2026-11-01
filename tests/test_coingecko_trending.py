"""CoinGecko trending snapshot (IDEA-20261028-coingecko-trending-attention-fade-forward, step 0): provider shape check,
the pure row builder (analytics/coingecko_trending.py), the table coingecko_trending and the collector job
`coingecko_trending`."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from conftest import State, make_transports
from tradedesk.analytics.coingecko_trending import MAX_ROWS, hour_ts, snapshot_rows
from tradedesk.collector.coingecko_trending import collect_coingecko_trending
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

FIXTURE = Path(__file__).parent / "fixtures" / "coingecko_trending_sample_2026-10-29.json"
SAMPLE = json.loads(FIXTURE.read_text())  # hand-built from the documented shape, NOT live (see `_source`); live: below
NOW = datetime(2026, 10, 29, 9, 41, 7, tzinfo=UTC)
HOUR = int(datetime(2026, 10, 29, 9, tzinfo=UTC).timestamp())
IDS = ["hyperliquid", "pudgy-penguins", "bitcoin", "zora", "freshly-listed-meme", "ethena"]


# ----------------------------------------------------------------------------- pure functions


def test_hour_ts_is_the_start_of_the_utc_hour():
    assert hour_ts(NOW) == HOUR
    assert hour_ts(datetime(2026, 10, 29, 9, 59, 59, 999_000, tzinfo=UTC)) == HOUR
    assert hour_ts(datetime(2026, 10, 29, 10, tzinfo=UTC)) == HOUR + 3600


def test_snapshot_rows_on_the_sample():
    rows = snapshot_rows(SAMPLE)
    assert [(r["rank"], r["coin_id"]) for r in rows] == list(enumerate(IDS, 1))
    assert [r["score"] for r in rows] == [0, 1, 2, 3, 4, 5]
    hype = rows[0]
    assert (hype["coin_num"], hype["symbol"], hype["name"], hype["market_cap_rank"]) == (50882, "HYPE", "Hyperliquid", 14)
    assert hype["price_usd"] == 38.12 and hype["price_btc"] == pytest.approx(0.000460724)
    assert hype["change_24h"] == pytest.approx(0.0527)  # USD percent -> decimal fraction
    assert hype["market_cap_usd"] == 12_734_601_288.0 and hype["volume_usd"] == 412_905_113.0  # "$12,734,601,288"
    assert rows[1]["change_24h"] == pytest.approx(-0.0784) and rows[1]["price_btc"] == pytest.approx(1.8123e-07)
    new = rows[4]
    assert new["market_cap_rank"] is None and new["market_cap_usd"] == 0.0  # unranked; "$0" kept as CoinGecko gives it
    assert new["price_usd"] == pytest.approx(2.566e-06) and new["change_24h"] == pytest.approx(4.128)


def test_snapshot_rows_on_the_live_sample():
    """Live answer taken on the server 2026-11-01 (scripts/_cg_live.py): 15 coins, ~60 currencies in the 24h change,
    an extra top-level `rwas` list; every column is filled."""
    live = json.loads((FIXTURE.parent / "coingecko_trending_live_2026-11-01.json").read_text())
    assert live["_source"].startswith("LIVE") and set(live) >= {"coins", "nfts", "categories", "rwas"}
    rows = snapshot_rows(live)
    assert [r["rank"] for r in rows] == list(range(1, 16))
    assert [r["score"] for r in rows] == list(range(15))
    assert all(v is not None for r in rows for v in r.values())
    btc = rows[0]
    item = live["coins"][0]["item"]
    assert (btc["coin_id"], btc["symbol"], btc["coin_num"], btc["market_cap_rank"]) == ("bitcoin", "BTC", 1, 1)
    assert btc["price_usd"] == pytest.approx(item["data"]["price"])
    assert btc["change_24h"] == pytest.approx(item["data"]["price_change_percentage_24h"]["usd"] / 100)
    assert btc["market_cap_usd"] == 1_563_309_615_981.0  # "$1,563,309,615,981"
    assert rows[1]["coin_id"] == "beam-2" and rows[1]["change_24h"] == pytest.approx(0.2504, abs=1e-4)


def test_snapshot_rows_survive_a_changed_item_shape():
    p = copy.deepcopy(SAMPLE)
    c = p["coins"]
    c[0]["item"]["data"] = "reshaped"  # no data block: market fields NULL, the row stays
    c[1]["item"]["price_btc"] = None  # falls back to data.price_btc (a string)
    c[1]["item"]["data"]["price"] = "$0.0<sub title='0.00000123'>5</sub>123"  # HTML display price -> NULL, not a guess
    c[1]["item"]["data"]["total_volume"] = "-$5"
    c[2] = {"item": {"symbol": "NOID"}}  # no id: skipped, rank 3 stays empty
    c[3]["item"]["id"] = "../../etc"  # not an id
    c[4]["item"]["market_cap_rank"] = True
    c[4]["item"]["score"] = "4"
    c[5] = c[0]  # a repeated id keeps its first position
    p["coins"] += [None, {"item": []}, {"item": {"id": "late-coin", "data": {"price": float("nan"), "market_cap": 7}}}]
    rows = snapshot_rows(p)
    by = {r["rank"]: r for r in rows}
    assert sorted(by) == [1, 2, 5, 9]
    assert by[1]["coin_id"] == "hyperliquid" and by[1]["price_usd"] is None and by[1]["change_24h"] is None
    assert by[1]["coin_num"] == 50882 and by[1]["market_cap_rank"] == 14  # item-level fields still read
    assert by[2]["price_btc"] == pytest.approx(1.8123e-07) and by[2]["price_usd"] is None and by[2]["volume_usd"] is None
    assert by[5]["market_cap_rank"] is None and by[5]["score"] is None
    assert by[9] == {"rank": 9, "coin_id": "late-coin", "coin_num": None, "symbol": None, "name": None, "market_cap_rank": None,
                     "score": None, "price_usd": None, "price_btc": None, "change_24h": None, "market_cap_usd": 7.0,
                     "volume_usd": None}  # fmt: skip
    for bad in (None, [], {"coins": {}}, {"coins": "x"}, {"error": "rate limited"}):
        assert snapshot_rows(bad) == []


def test_snapshot_rows_clean_untrusted_text_and_cap_length():
    p = copy.deepcopy(SAMPLE)
    p["coins"][0]["item"]["name"] = "Hyper‮liquid\x00\n" + "x" * 300  # bidi override, NUL, newline, very long
    p["coins"][0]["item"]["symbol"] = "​HYPE\x07"
    p["coins"][1]["item"]["symbol"] = 42
    rows = snapshot_rows(p)
    assert rows[0]["symbol"] == "HYPE" and rows[0]["name"].startswith("Hyperliquid") and len(rows[0]["name"]) == 100
    assert rows[1]["symbol"] is None
    many = {"coins": [{"item": {"id": f"c{i}"}} for i in range(MAX_ROWS + 20)]}
    assert len(snapshot_rows(many)) == MAX_ROWS


# ----------------------------------------------------------------------------- collector


def _rt(tmp_path, *, opts: dict | None = None, **kw) -> tuple[Runtime, list]:
    """Fake CoinGecko serving the sample at /search/trending. ``opts`` (read on every request): "status" (default 200),
    "body" (default the sample)."""
    calls: list = []
    opts = opts if opts is not None else {}

    def cg(req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/api/v3", 1)[-1]
        calls.append(path)
        if path != "/search/trending":
            return httpx.Response(404, text="not mocked")
        if opts.get("status", 200) != 200:
            return httpx.Response(opts["status"], text="down")
        return httpx.Response(200, json=opts.get("body", SAMPLE))

    tr = make_transports(State())
    tr["coingecko"] = httpx.MockTransport(cg)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite"), **kw), transports=tr)
    rt.now = lambda: NOW
    rt.coingecko.http._backoff = 0.0
    return rt, calls


async def test_job_stores_one_snapshot_per_hour(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_coingecko_trending(rt) == 6
        assert calls == ["/search/trending"]
        rows = await rt.store.coingecko_trending_rows()
        assert [(r["rank"], r["coin_id"]) for r in rows] == list(enumerate(IDS, 1))
        assert {r["ts"] for r in rows} == {HOUR} and {r["fetched_ts"] for r in rows} == {int(NOW.timestamp())}
        z = rows[3]
        assert (z["symbol"], z["name"], z["coin_num"], z["market_cap_rank"], z["score"]) == ("ZORA", "Zora", 54693, 312, 3)
        assert z["price_usd"] == pytest.approx(0.08616) and z["change_24h"] == pytest.approx(0.2305)
        assert (z["market_cap_usd"], z["volume_usd"]) == (286_110_957.0, 97_553_212.0)
        # a later tick in the same hour: no request, nothing written
        rt.now = lambda: NOW + timedelta(minutes=10)
        assert await collect_coingecko_trending(rt) == 0 and len(calls) == 1
        # the next hour: a new snapshot
        rt.now = lambda: NOW + timedelta(minutes=20)
        assert await collect_coingecko_trending(rt) == 6 and len(calls) == 2
        assert [r["ts"] for r in await rt.store.coingecko_trending_rows(coin_id="zora")] == [HOUR, HOUR + 3600]
    finally:
        await rt.aclose()


@pytest.mark.parametrize(
    ("opts", "field"),
    [
        ({"status": 500}, "coingecko_trending: backend_error"),
        ({"status": 429}, "coingecko_trending: rate_limited"),
        ({"body": {"trending": []}}, "coingecko_trending: backend_error unexpected /search/trending shape"),
        ({"body": ["bitcoin"]}, "coingecko_trending: backend_error unexpected /search/trending shape"),
        ({"body": {"coins": [{"item": {"symbol": "X"}}]}}, "coingecko_trending: data_unavailable no usable coin"),
        ({"body": {"coins": []}}, "coingecko_trending: data_unavailable no usable coin"),
    ],
)
async def test_failure_or_changed_shape_records_nothing_and_never_raises(tmp_path, caplog, opts, field):
    rt, _ = _rt(tmp_path, opts=opts)
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_coingecko_trending(rt) == 0
        assert await rt.store.coingecko_trending_rows() == []
        assert f"coingecko_trending: hour {HOUR} not recorded" in caplog.text and field in caplog.text
        opts.clear()
        rt.now = lambda: NOW + timedelta(minutes=10)  # recovers on the next tick of the same hour
        assert await collect_coingecko_trending(rt) == 6
    finally:
        await rt.aclose()


async def test_no_store_never_raises(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        rt.store = None
        assert await collect_coingecko_trending(rt) == 0 and calls == []
    finally:
        await rt.aclose()


async def test_insert_is_idempotent_for_the_same_hour(tmp_path):
    s = Store(tmp_path / "x.sqlite")
    await s.init()
    try:
        row = snapshot_rows(SAMPLE)[0]
        assert await s.insert_coingecko_trending(HOUR, HOUR + 60, [row, {"rank": 2}, {"coin_id": "x"}]) == 1  # no id/rank: dropped
        assert await s.insert_coingecko_trending(HOUR, HOUR + 900, [dict(row, coin_id="zora", price_usd=9.9)]) == 0  # first kept
        assert await s.coingecko_trending_has_hour(HOUR) and not await s.coingecko_trending_has_hour(HOUR + 3600)
        assert await s.coingecko_trending_rows() == [{"ts": HOUR, **row, "fetched_ts": HOUR + 60}]
        assert await s.insert_coingecko_trending(HOUR, HOUR + 900, [dict(row, rank=2, coin_id="zora")]) == 1  # a new rank
    finally:
        await s.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    s = Settings()
    assert s.collect_coingecko_trending_enabled is False and s.collect_coingecko_trending_seconds == 600
    assert allowed("TRADEDESK_COLLECT_COINGECKO_TRENDING_ENABLED") and allowed("TRADEDESK_COLLECT_COINGECKO_TRENDING_SECONDS")
    monkeypatch.delenv("TRADEDESK_COLLECT_COINGECKO_TRENDING_ENABLED", raising=False)
    assert Settings.from_env().collect_coingecko_trending_enabled is False
    monkeypatch.setenv("TRADEDESK_COLLECT_COINGECKO_TRENDING_ENABLED", "1")
    assert Settings.from_env().collect_coingecko_trending_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_coingecko_trending_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "coingecko_trending" not in names(off) and "coingecko_trending" in names(on)
    job = next(j for j in build_default_scheduler(on).jobs if j.name == "coingecko_trending")
    assert job.period_seconds == 600 and job.fn is collect_coingecko_trending


async def test_migration_34_adds_the_table_to_a_v33_store(tmp_path):
    path = tmp_path / "v33.sqlite"
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= 33:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 33")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 34
        assert await s.insert_coingecko_trending(HOUR, HOUR + 1, [{"rank": 1, "coin_id": "bitcoin", "symbol": "BTC"}]) == 1
        got = await s.coingecko_trending_rows()
        assert got == [{"ts": HOUR, "rank": 1, "coin_id": "bitcoin", "coin_num": None, "symbol": "BTC", "name": None,
                        "market_cap_rank": None, "score": None, "price_usd": None, "price_btc": None, "change_24h": None,
                        "market_cap_usd": None, "volume_usd": None, "fetched_ts": HOUR + 1}]  # fmt: skip
    finally:
        await s.aclose()
