"""Invariant 1: open findings of BACKLOG 6.53 that nobody had checked yet (item 296).

(a) plan_review and the hourly collector job plan_resolve (same `review_open`) on an open plan that another writer left
with garbage in entry / stop / target / created_ts / expires_ts (text, a blob, inf, the text "NaN", 1e308, 1e20 — the
store cannot hold 10**20 as INTEGER, so it keeps it as REAL — entry = stop, entry <= 0) or in rec_paper_id: the bad plan
is skipped with a warning naming it, stays open and unfilled, and the good plan of the same market is still resolved.
Before item 296 float() / int() of a text level or time raised out of `resolve_plan` (ValueError), a blob or text level
raised in `_view` of the `open` list, entry = stop raised ZeroDivisionError in `r_multiple`, inf created_ts raised
OverflowError, and a rec_paper_id of 1e308 raised OverflowError out of the store lookup; any of them failed the whole
tool and the job, so no other plan was scored. plan_link on such rows was already covered by item 289
(test_tool_failure_extra_7.py); one more case here. (b) iv_rank with +-1e308 in atm_iv returned NaN (inf / inf) or a
wrong rank of 0 (finite / inf). (c) positions_list returned NaN / Infinity from tags "[NaN]" / "[Infinity]".

The in-memory store is reopened empty on every MCP connection, so each test makes its calls and store checks in one
session.
"""

from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.trade_plan import collect_plan_resolve
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

NOW_TS = int(NOW.timestamp())
HUGE = 1e308
INF = float("inf")


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


def _no_constant(name: str) -> None:
    raise AssertionError(f"{name} in the reply")


@asynccontextmanager
async def _session(rt):
    async with Client(build_server(rt)[0]) as c:

        async def call(tool: str, /, **args) -> dict:
            res = await c.call_tool(tool, args)
            assert not res.is_error, res.content[0].text[:300] if res.content else ""
            # The text reply keeps Infinity / NaN (the structured one shows null): strict JSON or fail. A stored raw value
            # shown as text (`expires_raw` "inf") is fine, so this checks the JSON tokens rather than `first_nonfinite`.
            text = json.loads(res.content[0].text, parse_constant=_no_constant)
            data = res.structured_content if res.structured_content is not None else text
            return data["result"] if set(data) == {"result"} else data

        yield call


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


# ---------- (a) plan_review / plan_resolve on a corrupt open plan ----------

PLAN_NAMES = ["id", "created_ts", "exchange", "symbol", "instrument", "side", "entry_type", "entry", "stop", "target"]
PLAN_NAMES += ["expires_ts", "verdict", "tags", "rules", "rec_paper_id", "status"]
PLAN_SQL = f"INSERT INTO trade_plan ({', '.join(PLAN_NAMES)}) VALUES ({', '.join('?' * len(PLAN_NAMES))})"
# FakeEx 1h candles rise 0.1 % an hour from 80080 (05:00) to 12:00: this long market plan reaches its target.
GOOD_PLAN = (1, NOW_TS - 8 * 3600, "binance", "BTC/USDT:USDT", "perp", "long", "market", 80100.0, 79000.0, 80500.0)
GOOD_PLAN += (NOW_TS + 86400, "enter", '["src:own"]', '{"5": "pass"}', None, "open")


def _plan(**cols) -> tuple:
    row = dict(zip(PLAN_NAMES, GOOD_PLAN, strict=True)) | {"id": 2} | cols
    return tuple(row[n] for n in PLAN_NAMES)


BAD_PLANS = {
    **{f"entry_{k}": {"entry": v} for k, v in (("text", "abc"), ("nan_text", "NaN"), ("blob", b"\x00\x01"), ("utf8", b"\xff\xfe"))},
    "entry_inf": {"entry": INF},
    "entry_huge": {"entry": HUGE},
    "entry_zero": {"entry": 0.0},
    "entry_negative": {"entry": -80100.0},
    "entry_eq_stop": {"stop": 80100.0},
    "stop_text": {"stop": "abc"},
    "stop_inf": {"stop": INF},
    "stop_huge": {"stop": HUGE},
    "target_text": {"target": "abc"},
    "target_neg_inf": {"target": -INF},
    "target_huge": {"target": HUGE},
    "all_levels_huge": {"entry": HUGE, "stop": 0.99 * HUGE, "target": HUGE},
    "created_text": {"created_ts": "abc"},
    "created_inf": {"created_ts": INF},
    "created_1e20": {"created_ts": 1e20},
    "created_huge": {"created_ts": HUGE},
    "expires_1e20": {"expires_ts": 1e20},
    "expires_huge": {"expires_ts": HUGE},
    "expires_inf": {"expires_ts": INF},
    "side_blob": {"side": b"\xff"},
    "side_text": {"side": "sideways"},
    "entry_type_text": {"entry_type": "iceberg"},
    "rec_paper_text": {"rec_paper_id": "abc", "instrument": "option"},
    "rec_paper_huge": {"rec_paper_id": HUGE, "instrument": "option"},
}


async def _plan_row(rt, pid: int) -> tuple:
    rows = await rt.store.conn.execute_fetchall("SELECT status, fill, r_result FROM trade_plan WHERE id = ?", (pid,))
    return tuple(rows[0])


@pytest.mark.parametrize("cols", BAD_PLANS.values(), ids=BAD_PLANS.keys())
async def test_plan_review_skips_a_corrupt_open_plan(rt, cols):
    await _rows(rt, PLAN_SQL, [GOOD_PLAN, _plan(**cols)])
    async with _session(rt) as call:
        env = await call("plan_review")
        assert env["ok"] is True and env["data"]["resolved_now"] == [1], str(env)[:600]
        assert any("plan 2 skipped" in w for w in env["warnings"]), env["warnings"]
        by = {p["id"]: p for p in env["data"]["recent_resolved"]}
        assert by[1]["outcome"] == "target" and math.isfinite(by[1]["r_result"])
        assert 1 not in [p["id"] for p in env["data"]["open"]]
        assert await _plan_row(rt, 2) == ("open", None, None)  # neither resolved nor filled


@pytest.mark.parametrize("cols", BAD_PLANS.values(), ids=BAD_PLANS.keys())
async def test_plan_resolve_job_skips_a_corrupt_open_plan(rt, cols):
    # The hourly job raised with the tool before item 296: one bad row stopped the scoring of every other plan.
    await _rows(rt, PLAN_SQL, [GOOD_PLAN, _plan(**cols)])
    assert await collect_plan_resolve(rt) == 1
    assert (await _plan_row(rt, 1))[0] == "resolved"
    assert await _plan_row(rt, 2) == ("open", None, None)


async def test_plan_review_corrupt_plan_alone_still_answers(rt):
    await _rows(rt, PLAN_SQL, [_plan(id=1, entry="abc")])
    async with _session(rt) as call:
        env = await call("plan_review")
        assert env["ok"] is True and env["data"]["resolved_now"] == [] and env["data"]["open"] == []
        assert any("plan 1 skipped" in w for w in env["warnings"])
        assert any(w.startswith("1 stored plan rows skipped") for w in env["warnings"])  # left out of `open` too


@pytest.mark.parametrize("tags", ['["src:own"]', '["src:demo-shadow"]'], ids=["own", "shadow"])
@pytest.mark.parametrize("r_result", ["abc", b"\xff\xfe", INF])
async def test_plan_review_leaves_a_corrupt_resolved_plan_out_of_the_summary(rt, tags, r_result):
    # `_view` of recent_resolved and float() in the summary raised on a text / blob r_result before item 296.
    sql = PLAN_SQL.replace("status)", "status, outcome, r_result, resolved_ts)").replace("?)", "?,?,?,?)")
    await _rows(rt, sql, [(*_plan(id=3, status="resolved", tags=tags), "target", r_result, NOW_TS - 60)])
    async with _session(rt) as call:
        env = await call("plan_review")
        assert env["ok"] is True and env["data"]["recent_resolved"] == []
        assert env["data"]["summary"]["by_verdict"]["enter"]["n"] == 0 and env["data"]["demo_shadow"]["by_verdict"]["enter"]["n"] == 0
        assert any(w.startswith("1 stored plan rows skipped") and "[3]" in w for w in env["warnings"]), env["warnings"]


async def test_plan_review_extreme_but_sane_levels_are_still_scored(rt):
    # A distant target is not garbage: the plan is scored (expires later, no touch yet) and nothing is skipped.
    await _rows(rt, PLAN_SQL, [GOOD_PLAN, _plan(target=80500.0 * 500, symbol="BTC/USDT:USDT")])
    async with _session(rt) as call:
        env = await call("plan_review")
        assert env["ok"] is True and env["data"]["resolved_now"] == [1]
        assert not any("skipped" in w for w in env["warnings"]), env["warnings"]
        assert [p["id"] for p in env["data"]["open"]] == [2] and env["data"]["open"][0]["fill"] == 80100.0


async def test_plan_link_on_a_nan_text_level_links_without_the_row(rt):
    await _rows(rt, PLAN_SQL, [_plan(id=1, entry="NaN", created_ts=1e20)])
    await _rows(rt, "INSERT INTO journal (id, opened_ts, symbol, side, size, entry, status) VALUES (?,?,?,?,?,?,?)",
                [(1, NOW_TS - 30, "BTC/USDT:USDT", "long", 1.0, 100.0, "open")])  # fmt: skip
    async with _session(rt) as call:
        env = await call("plan_link", id=1, journal_id=1)
        assert env["ok"] is True and env["data"] == {"id": 1, "journal_id": 1}
        assert any("its stored row is malformed" in w for w in env["warnings"])


# ---------- (b) iv_rank on +-1e308 ----------

EXPIRY = "2026-11-27T08:00:00+00:00"


async def _atm_iv(rt, ivs: list) -> None:
    n = len(ivs)
    await _rows(
        rt,
        "INSERT INTO atm_term VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(NOW_TS - (n - 1 - i) * 3600, "BTC", EXPIRY, 48.0, 83000.0, v, -0.02, 0.01, None, None) for i, v in enumerate(ivs)],
    )


BASE_IVS = [0.40 + 0.01 * i for i in range(10)]


@pytest.mark.parametrize(
    ("ivs", "rank"),
    [
        (BASE_IVS + [-HUGE, HUGE], 1.0),  # inf / inf = NaN before item 296
        (BASE_IVS + [HUGE, -HUGE], 0.0),
        ([HUGE, -HUGE, *BASE_IVS, 0.45], 0.5),  # finite / inf = 0.0 before item 296 (the true rank is 0.5)
        (BASE_IVS + [HUGE], 1.0),
        ([-HUGE, *BASE_IVS], 1.0),
    ],
    ids=["latest_max", "latest_min", "latest_mid", "one_huge", "one_neg_huge"],
)
async def test_iv_rank_on_huge_atm_iv_is_finite(rt, ivs, rank):
    await _atm_iv(rt, ivs)
    async with _session(rt) as call:
        env = await call("iv_rank", currency="BTC", expiry=EXPIRY, lookback_days=1)
        d = env["data"]
        assert env["ok"] is True and d["count"] == len(ivs), str(env)[:400]
        assert d["iv_rank"] == pytest.approx(rank, abs=1e-4)
        assert 0.0 <= d["percentile"] <= 1.0


async def test_iv_rank_ordinary_values_unchanged(rt):
    await _atm_iv(rt, BASE_IVS + [0.45])
    async with _session(rt) as call:
        d = (await call("iv_rank", currency="BTC", expiry=EXPIRY, lookback_days=1))["data"]
        assert d["iv_rank"] == pytest.approx(round((0.45 - 0.40) / (0.49 - 0.40), 4)) and d["min_iv"] == pytest.approx(0.40)


# ---------- (c) positions_list on NaN / Infinity tags ----------

POS_SQL = "INSERT INTO positions (opened_ts, instrument, kind, side, size, mark, tags) VALUES (?,?,?,?,?,?,?)"
GOOD_POS = (NOW_TS - 10, "BTC-PERP", "perp", "long", 2.0, 100.0, '["core"]')


@pytest.mark.parametrize("tags", ["[NaN]", "[Infinity]", "[-Infinity]", '["a", 1e400]', '{"x": NaN}'])
async def test_positions_list_skips_non_finite_tags(rt, tags):
    await _rows(rt, POS_SQL, [GOOD_POS, (NOW_TS, "ETH-PERP", "perp", "long", 1.0, 100.0, tags)])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and env["data"]["count"] == 1, str(env)[:400]
        assert env["data"]["positions"][0]["instrument"] == "BTC-PERP" and env["data"]["positions"][0]["tags"] == ["core"]
        assert any(w.startswith("1 stored position rows skipped") and "tags" in w for w in env["warnings"]), env["warnings"]
