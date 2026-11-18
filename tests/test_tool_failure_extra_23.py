"""Invariant 1: plan_* and carry_paper_* on a stored `tags` cell that is not a JSON list of strings, and assistant_paper_*
on a stored `legs` cell that is not a list of legs (item 356; found by a trial run in item 355).

Found: tags nested 300 levels deep parsed fine (item 355 covered only the depth where json.loads raises) but failed the
MCP serialiser ("Circular reference detected (depth exceeded)") of plan_list / plan_review / plan_link and
carry_paper_status / carry_paper_close; carry_paper_close (removed 2026-11-18, with carry_paper_open) also copied them
into the journal row it writes, and a bare number in tags raised TypeError there (and in plan_review's summary by tag). A rec_paper `legs` that is not a list
(None after parsing, a number, a nested list) raised TypeError / ValueError in `_view` of assistant_paper_status, and in
the settlement of a due trade.

Now: plans use the same clean_tags as journal_* / positions_* (non-list or non-strings -> their string items, warning;
NaN / Infinity -> the plan is skipped, as before). Carry pairs are never hidden: tags are normalised the same way and
NaN / Infinity tags read as [] with a warning; the journal mirror gets only string tags. assistant_paper_status shows
only well-formed legs (warning) and leaves a due trade with malformed legs open instead of settling it on garbage.

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from conftest import NOW, FakeEx, State, make_transports
from test_tool_failure_extra_19 import NORMALISED, NORMALISED_IDS, _deep, _session
from tradedesk.config import Settings
from tradedesk.runtime import Runtime

NOW_TS = int(NOW.timestamp())
NAN_TAGS = ["[NaN]", '["a", {"b": [Infinity]}]', "[" * 300 + "NaN" + "]" * 300]
NAN_IDS = ["nan", "deep_obj_inf", "deep300_nan"]
TAGS_WARNING = "had tags that were not a list of strings"


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


async def _set(rt, table: str, col: str, value, row_id: int) -> None:
    await rt.store.conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (value, row_id))
    await rt.store.conn.commit()


# ---------- trade plans ----------

PLAN = {
    "exchange": "binance",
    "symbol": "BTC/USDT:USDT",
    "instrument": "perp",
    "side": "long",
    "entry_type": "limit",
    "entry": 100.0,
    "stop": 90.0,
    "target": 130.0,
    "expires_ts": NOW_TS + 86_400,
    "verdict": "wait",
    "tags": ["src:own"],
}
RESOLVED = (
    "UPDATE trade_plan SET status = 'resolved', outcome = 'target', resolved_ts = ?, filled_ts = ?, fill = 100.0, exit = 130.0, "
    "r_result = 2.9, timeframe = '1h' WHERE id = ?"
)


@pytest.mark.parametrize(("tags", "shown"), NORMALISED, ids=NORMALISED_IDS)
async def test_plan_list_normalises_non_string_tags(rt, tags, shown):
    async with _session(rt) as call:
        good = (await call("plan_add", **PLAN))["data"]["id"]
        bad = (await call("plan_add", **PLAN))["data"]["id"]
        await _set(rt, "trade_plan", "tags", tags, bad)
        env = await call("plan_list")
        assert env["ok"] is True and env["missing"] == [] and env["data"]["count"] == 2
        assert {p["id"]: p["tags"] for p in env["data"]["plans"]} == {good: ["src:own"], bad: shown}
        assert any(f"1 stored plan rows {TAGS_WARNING}" in w and f"[{bad}]" in w for w in env["warnings"])


@pytest.mark.parametrize("tags", NAN_TAGS, ids=NAN_IDS)
async def test_plan_list_still_skips_non_finite_tags(rt, tags):
    async with _session(rt) as call:
        good = (await call("plan_add", **PLAN))["data"]["id"]
        bad = (await call("plan_add", **PLAN))["data"]["id"]
        await _set(rt, "trade_plan", "tags", tags, bad)
        env = await call("plan_list")
        assert env["ok"] is True and [p["id"] for p in env["data"]["plans"]] == [good]
        assert any("1 stored plan rows skipped" in w and f"[{bad}]" in w for w in env["warnings"])


@pytest.mark.parametrize(("tags", "shown"), [("5", []), ('["a", 1, ["b"]]', ["a"]), (_deep(300), [])], ids=["number", "mixed", "deep300"])
async def test_plan_review_summary_by_tag_on_non_string_tags(rt, tags, shown):
    # A number in tags raised TypeError in summarize_plans (sorted() of mixed types), a nested list "unhashable type".
    async with _session(rt) as call:
        ids = [(await call("plan_add", **PLAN))["data"]["id"] for _ in range(2)]
        for i in ids:
            await rt.store.conn.execute(RESOLVED, (NOW_TS - 60, NOW_TS - 600, i))
        await rt.store.conn.commit()
        await _set(rt, "trade_plan", "tags", tags, ids[1])
        env = await call("plan_review")
        assert env["missing"] == [] and env["data"]["summary"]["by_verdict"]["wait"]["n"] == 2
        assert set(env["data"]["summary"]["by_tag"]) == {"src:own", *shown}
        assert {p["id"]: p["tags"] for p in env["data"]["recent_resolved"]} == {ids[0]: ["src:own"], ids[1]: shown}
        assert any(TAGS_WARNING in w for w in env["warnings"])


async def test_plan_link_returns_the_row_with_normalised_tags(rt):
    async with _session(rt) as call:
        pid = (await call("plan_add", **PLAN))["data"]["id"]
        jid = (await call("journal_add", symbol="BTC/USDT:USDT", side="long", size=1.0, entry=100.0, stop=90.0))["data"]["id"]
        await _set(rt, "trade_plan", "tags", _deep(300), pid)
        env = await call("plan_link", id=pid, journal_id=jid)
        assert env["ok"] is True and (env["data"]["id"], env["data"]["journal_id"], env["data"]["tags"]) == (pid, jid, [])


async def test_plan_tags_valid_unchanged_and_store_failure(rt, monkeypatch):
    async with _session(rt) as call:
        pid = (await call("plan_add", **{**PLAN, "tags": [" src:own ", "", "setup:S3"]}))["data"]["id"]
        env = await call("plan_list")
        assert env["ok"] is True and env["data"]["plans"][0]["tags"] == ["src:own", "setup:S3"]
        assert not any(TAGS_WARNING in w for w in env["warnings"])
        await _set(rt, "trade_plan", "tags", "null", pid)
        assert (await call("plan_list"))["data"]["plans"][0]["tags"] is None

        async def boom(**_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rt.store, "plan_list", boom)
        env = await call("plan_list")
        assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]


# ---------- carry_paper ----------


async def _open_pair(rt) -> int:
    """An open pair as carry_paper_open (removed 2026-11-18) recorded it three days ago, written through the store."""
    return await rt.store.carry_paper_insert(
        opened_ts=NOW_TS - 3 * 86_400, base="BTC", symbol="BTC/USDT:USDT", long_venue="binanceusdm", short_venue="bybit",
        qty=1000 / 82750.0, notional_usd=1000.0, long_entry=82750.0, short_entry=82750.0, leverage=2.0, stop_move=0.5,
        lookback_days=7.0, exit_apr=0.0, slippage=0.0005, tags=["t1"],
    )  # fmt: skip


@pytest.mark.parametrize(("tags", "shown"), NORMALISED, ids=NORMALISED_IDS)
async def test_carry_paper_status_normalises_tags(rt, tags, shown):
    async with _session(rt) as call:
        pid = await _open_pair(rt)
        await _set(rt, "carry_paper", "tags", tags, pid)
        env = await call("paper_carry_status", strategy="manual")
        (pair,) = env["data"]["open"]
        assert env["ok"] is True and pair["id"] == pid and "mark" in pair and pair["tags"] == shown
        assert any(f"paper pair id {[pid]} {TAGS_WARNING}" in w for w in env["warnings"])


@pytest.mark.parametrize("tags", NAN_TAGS, ids=NAN_IDS)
async def test_carry_paper_status_non_finite_tags_read_as_empty(rt, tags):
    async with _session(rt) as call:
        pid = await _open_pair(rt)
        await _set(rt, "carry_paper", "tags", tags, pid)
        env = await call("paper_carry_status", strategy="manual")
        assert env["ok"] is True and [(p["id"], p["tags"]) for p in env["data"]["open"]] == [(pid, [])]
        assert any(f"paper pair id {[pid]} had NaN / Infinity in tags" in w for w in env["warnings"])


async def test_carry_paper_single_status_with_deep_tags(rt):
    async with _session(rt) as call:
        pid = await _open_pair(rt)
        await _set(rt, "carry_paper", "tags", _deep(300), pid)
        await _set(rt, "carry_paper", "status", "cancelled", pid)
        env = await call("paper_carry_status", strategy="manual", id=pid)
        assert env["ok"] is True and (env["data"]["trade"]["id"], env["data"]["trade"]["tags"]) == (pid, [])


async def test_carry_paper_valid_tags_unchanged_and_store_failure(rt, monkeypatch):
    async with _session(rt) as call:
        pid = await _open_pair(rt)
        env = await call("paper_carry_status", strategy="manual")
        assert env["data"]["open"][0]["tags"] == ["t1"] and not any("tags" in w for w in env["warnings"])
        await _set(rt, "carry_paper", "tags", "null", pid)
        env = await call("paper_carry_status", strategy="manual")
        assert env["data"]["open"][0]["tags"] == [] and not any("tags" in w for w in env["warnings"])

        async def boom(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rt.store, "carry_paper_get", boom)
        env = await call("paper_carry_status", strategy="manual", id=pid)
        assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]


# ---------- assistant_paper (rec_paper legs) ----------

GOOD_LEGS = [[90_000.0, False, 1.0, 0.5, 1200.0], [110_000.0, True, 1.0, 0.5, 1300.0]]
BAD_LEGS = [
    ("null", []),
    ("5", []),
    ("{}", []),
    ('"x"', []),
    ("[[1, 2]]", []),
    ('[["a", true, 1.0, 0.5, 1.0]]', []),
    ("[" + json.dumps(GOOD_LEGS[0]) + ', "junk", [1, true, 1, "x", 2]]', [0]),
    (_deep(300), []),
]
BAD_LEG_IDS = ["null", "number", "object", "string", "short_leg", "text_strike", "one_good", "deep300"]


def _paper_rt(tmp_path) -> Runtime:
    tr = make_transports(State())
    base = tr["deribit"].handler

    def deribit(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("get_delivery_prices"):
            rows = [{"date": "2026-11-27", "delivery_price": 100_000.0}]
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"data": rows, "records_total": 1}})
        return base(req)

    tr["deribit"] = httpx.MockTransport(deribit)
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / "rec.sqlite")), transports=tr, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


async def _rec(rt, legs_json: str | None = None) -> int:
    await rt.store.init()
    tid = await rt.store.rec_paper_insert(
        opened_ts=NOW_TS - 86_400,
        currency="BTC",
        structure="long_strangle25",
        expiry_ts=int(datetime(2026, 11, 27, 8, tzinfo=UTC).timestamp()),
        spot=100_000.0,
        forward=100_100.0,
        t_years=0.1,
        legs=GOOD_LEGS,
        premium_per_btc=2500.0,
        entry_fees_per_btc=60.0,
        max_loss_per_btc=2560.0,
        risk_basis="max_loss",
        size_btc=0.1,
        deposit_usd=100_000.0,
        risk_pct=1.0,
        risk_usd=256.0,
        within_risk=True,
        source="manual",
    )
    if legs_json is not None:
        await rt.store.conn.execute("UPDATE rec_paper SET legs = ? WHERE id = ?", (legs_json, tid))
        await rt.store.conn.commit()
    return tid


@pytest.mark.parametrize(("legs", "kept"), BAD_LEGS, ids=BAD_LEG_IDS)
async def test_assistant_paper_status_shows_only_well_formed_legs(tmp_path, legs, kept):
    rt = _paper_rt(tmp_path)
    try:
        good, bad = await _rec(rt), await _rec(rt, legs)
        async with _session(rt) as call:
            env = await call("assistant_paper_status")
            assert env["ok"] is True and env["missing"] == []
            views = {t["id"]: t for t in env["data"]["open"]}
            assert len(views[good]["legs"]) == 2 and [x["strike"] for x in views[bad]["legs"]] == [GOOD_LEGS[i][0] for i in kept]
            assert any(f"paper trade(s) {[bad]}" in w and "legs" in w for w in env["warnings"])
    finally:
        await rt.aclose()


@pytest.mark.parametrize(("legs", "kept"), BAD_LEGS, ids=BAD_LEG_IDS)
async def test_assistant_paper_status_leaves_a_due_trade_with_bad_legs_open(tmp_path, legs, kept):
    rt = _paper_rt(tmp_path)
    try:
        good, bad = await _rec(rt), await _rec(rt, legs)
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        async with _session(rt) as call:
            env = await call("assistant_paper_status")
            assert env["ok"] is True
            assert [t["id"] for t in env["data"]["open"]] == [bad] and [t["id"] for t in env["data"]["settled"]] == [good]
            assert any(f"rec_paper {bad} not settled" in w for w in env["warnings"])
            assert env["data"]["summary_settled"]["manual"]["n"] == 1
    finally:
        await rt.aclose()


async def test_assistant_paper_status_valid_legs_no_warning_and_store_failure(tmp_path, monkeypatch):
    rt = _paper_rt(tmp_path)
    try:
        await _rec(rt)
        async with _session(rt) as call:
            env = await call("assistant_paper_status")
            assert env["ok"] is True and len(env["data"]["open"][0]["legs"]) == 2 and not any("legs" in w for w in env["warnings"])

            async def boom(**_k):
                raise sqlite3.OperationalError("disk I/O error")

            monkeypatch.setattr(rt.store, "rec_paper_list", boom)
            env = await call("assistant_paper_status")
            assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]
    finally:
        await rt.aclose()
