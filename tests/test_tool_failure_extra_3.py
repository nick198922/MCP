"""Invariant 1 / 5: store failure, corrupt stored rows or degenerate inputs -> envelope, never an exception (item 280,
BACKLOG 6.53).

Five store tools that only the generic sweeps covered: alerts_recent, journal_stats, positions_risk, positions_list,
plan_list (touch_probability, named in the task, already has its own degenerate-input tests in
test_calc_degenerate_inputs.py / test_tools_bad_input.py, so positions_list took its place). Plus the two findings left
open by item 279: iv_rank and ohlcv_history on corrupt rows. Each tool gets a failing read query and rows that another
writer left with a string, a blob (valid and invalid UTF-8) or inf in a numeric column. Found on the way: plan_list,
journal_stats and iv_rank raised (TypeError / ValueError / ZeroDivisionError); an invalid UTF-8 blob failed the JSON reply
of alerts_recent and ohlcv_history; inf gave Infinity / NaN in journal_stats, positions_risk and ohlcv_history. Such rows
are now skipped with a warning; an all-malformed window is ok=false.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools._util import first_nonfinite

EXPIRY = "2026-11-27T08:00:00+00:00"
NOW_TS = int(NOW.timestamp())
CORRUPT = ("abc", b"\x00\x01", b"\xff\xfe", float("inf"))


async def _call(srv, name: str, **args) -> dict:
    async with Client(srv) as c:
        res = await c.call_tool(name, args)
    assert not res.is_error, res.content[0].text[:300] if res.content else ""
    text = json.loads(res.content[0].text)  # the text reply keeps Infinity / NaN, the structured one shows them as null
    assert first_nonfinite(text.get("data")) is None, str(text)[:300]
    data = res.structured_content if res.structured_content is not None else text
    return data["result"] if set(data) == {"result"} else data


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


def _read_fails(rt, monkeypatch, method: str) -> None:
    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.store, method, boom)


def _store_failed(env: dict) -> None:
    assert env["ok"] is False and env["data"] is None
    assert any(m["field"] == "store" and m["reason"] == "backend_error" and "disk I/O error" in m["detail"] for m in env["missing"])


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


def _skipped(env: dict, what: str, n: int = 1) -> bool:
    return any(w.startswith(f"{n} stored {what} rows skipped") for w in env["warnings"])


# ---------- alerts_recent ----------

ALERT_SQL = "INSERT INTO alerts (rule_id, ts, payload, channel, delivered, delivery_error) VALUES (?,?,?,?,?,?)"
GOOD_ALERT = ("r1", NOW_TS, json.dumps({"metric": "funding", "value": 0.1}), "log", 1, None)


async def test_alerts_recent_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "recent_alerts")
    _store_failed(await _call(build_server(rt)[0], "alerts_recent"))


@pytest.mark.parametrize("bad", CORRUPT)
async def test_alerts_recent_skips_a_corrupt_timestamp(rt, bad):
    await _rows(rt, ALERT_SQL, [GOOD_ALERT, ("r2", bad, "{}", "log", 0, None)])
    env = await _call(build_server(rt)[0], "alerts_recent")
    assert env["ok"] is True and env["data"]["count"] == 1 and env["data"]["alerts"][0]["rule_id"] == "r1"
    assert env["data"]["alerts"][0]["payload"] == {"metric": "funding", "value": 0.1}
    assert _skipped(env, "alert")


async def test_alerts_recent_skips_an_invalid_utf8_blob_payload(rt):
    # Before item 280 the blob reached the JSON reply and the whole call failed with a serialization error.
    await _rows(rt, ALERT_SQL, [GOOD_ALERT, ("r2", NOW_TS - 5, b"\xff\xfe", "log", 0, b"\xff")])
    env = await _call(build_server(rt)[0], "alerts_recent")
    assert env["ok"] is True and env["data"]["count"] == 1 and _skipped(env, "alert")


async def test_alerts_recent_empty_history_is_ok_without_a_warning(rt):
    env = await _call(build_server(rt)[0], "alerts_recent")
    assert env["ok"] is True and env["data"] == {"count": 0, "alerts": []} and env["warnings"] == []


# ---------- journal_stats ----------

JOURNAL_SQL = "INSERT INTO journal (opened_ts, symbol, side, size, entry, status, outcome, pnl, r_multiple) VALUES (?,?,?,?,?,?,?,?,?)"
GOOD_TRADES = [
    (NOW_TS - 300, "BTC/USDT:USDT", "long", 1.0, 100.0, "closed", "win", 10.0, 1.0),
    (NOW_TS - 200, "BTC/USDT:USDT", "long", 1.0, 100.0, "closed", "loss", -5.0, -0.5),
]


async def test_journal_stats_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "journal_list")
    _store_failed(await _call(build_server(rt)[0], "journal_stats"))


@pytest.mark.parametrize("col", ["pnl", "r_multiple"])
@pytest.mark.parametrize("bad", CORRUPT)
async def test_journal_stats_skips_a_corrupt_trade(rt, bad, col):
    # Before item 280 float() in summary() raised ValueError on the string / blob; inf gave Infinity totals.
    pnl, rm = (bad, 1.0) if col == "pnl" else (3.0, bad)
    await _rows(rt, JOURNAL_SQL, [*GOOD_TRADES, (NOW_TS - 100, "BTC/USDT:USDT", "long", 1.0, 100.0, "closed", "win", pnl, rm)])
    env = await _call(build_server(rt)[0], "journal_stats")
    d = env["data"]
    assert env["ok"] is True and d["closed_trades"] == 2 and d["wins"] == 1 and d["losses"] == 1
    assert d["total_pnl"] == pytest.approx(5.0) and d["avg_r"] == pytest.approx(0.25) and d["r_count"] == 2
    assert _skipped(env, "closed trade")


async def test_journal_stats_only_corrupt_trades_is_not_ok(rt):
    # All-malformed must not read as "no closed trades" (an empty journal stays ok=true with closed_trades 0).
    await _rows(rt, JOURNAL_SQL, [(NOW_TS, "BTC/USDT:USDT", "long", 1.0, 100.0, "closed", "win", "abc", None)])
    env = await _call(build_server(rt)[0], "journal_stats")
    assert env["ok"] is False and env["data"] is None
    assert any(m["field"] == "journal" and m["reason"] == "data_unavailable" and "malformed" in m["detail"] for m in env["missing"])


async def test_journal_stats_null_r_multiple_is_not_corrupt(rt):
    # A trade closed without a stop has r_multiple NULL: counted in PnL, not in R, and no warning.
    await _rows(rt, JOURNAL_SQL, [*GOOD_TRADES, (NOW_TS - 100, "BTC/USDT:USDT", "long", 1.0, 100.0, "closed", "win", 3.0, None)])
    env = await _call(build_server(rt)[0], "journal_stats")
    assert env["ok"] is True and env["data"]["closed_trades"] == 3 and env["data"]["r_count"] == 2 and env["warnings"] == []


# ---------- positions_risk / positions_list ----------

POS_SQL = "INSERT INTO positions (opened_ts, instrument, kind, side, size, mark, delta, gamma, vega, theta) VALUES (?,?,?,?,?,?,?,?,?,?)"
GOOD_POS = (NOW_TS - 10, "BTC-PERP", "perp", "long", 2.0, 100.0, 1.0, 0.0, 0.0, 0.0)


async def test_positions_risk_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "positions_list")
    _store_failed(await _call(build_server(rt)[0], "positions_risk"))


@pytest.mark.parametrize("col", [5, 6, 4])  # mark, delta, size
@pytest.mark.parametrize("bad", CORRUPT)
async def test_positions_risk_leaves_out_a_corrupt_position(rt, bad, col):
    # Before item 280 a string / blob greek counted as 0 without a word and inf gave Infinity / NaN net greeks.
    row = list(GOOD_POS)
    row[1], row[col] = "ETH-PERP", bad
    await _rows(rt, POS_SQL, [GOOD_POS, tuple(row)])
    env = await _call(build_server(rt)[0], "positions_risk")
    assert env["ok"] is True
    g = env["data"]["net_greeks"]
    assert g["count"] == 1 and g["delta"] == pytest.approx(2.0) and g["notional_usd"] == pytest.approx(200.0)
    assert list(env["data"]["per_instrument"]) == ["BTC-PERP"]
    assert _skipped(env, "open position")


async def test_positions_risk_only_corrupt_positions_is_not_ok(rt):
    await _rows(rt, POS_SQL, [(NOW_TS, "BTC-PERP", "perp", "long", 1.0, 100.0, "abc", 0.0, 0.0, 0.0)])
    env = await _call(build_server(rt)[0], "positions_risk")
    assert env["ok"] is False and env["data"] is None
    assert any(m["field"] == "positions" and "malformed" in m["detail"] for m in env["missing"])


@pytest.mark.parametrize(
    "args",
    [{"days": "inf"}, {"reference_spot": "1e400"}, {"scenarios": [{"d_spot_pct": "inf"}]}, {"scenarios": [{"d_vol_points": "nan"}]}],
)
async def test_positions_risk_non_finite_input_is_invalid(rt, args):
    await _rows(rt, POS_SQL, [GOOD_POS])
    env = await _call(build_server(rt)[0], "positions_risk", **args)
    assert env["ok"] is False and env["data"] is None and env["missing"][0]["reason"] == "invalid_input"


async def test_positions_risk_overflowing_result_is_not_ok(rt):
    # Finite stored greeks whose product overflows: ok=false instead of Infinity in the totals.
    await _rows(rt, POS_SQL, [(NOW_TS, "BTC-PERP", "perp", "long", 10.0, 100.0, 1e308, 0.0, 0.0, 0.0)])
    env = await _call(build_server(rt)[0], "positions_risk")
    assert env["ok"] is False and env["data"] is None
    assert any(m["field"] == "result" and m["reason"] == "invalid_input" for m in env["missing"])


async def test_positions_list_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "positions_list")
    _store_failed(await _call(build_server(rt)[0], "positions_list"))


@pytest.mark.parametrize("bad", CORRUPT)
async def test_positions_list_skips_a_corrupt_position(rt, bad):
    await _rows(rt, POS_SQL, [GOOD_POS, (NOW_TS, "ETH-PERP", "perp", "long", 1.0, bad, 1.0, 0.0, 0.0, 0.0)])
    env = await _call(build_server(rt)[0], "positions_list")
    assert env["ok"] is True and env["data"]["count"] == 1 and env["data"]["positions"][0]["instrument"] == "BTC-PERP"
    assert _skipped(env, "position")


# ---------- plan_list ----------

PLAN_SQL = (
    "INSERT INTO trade_plan (created_ts, exchange, symbol, instrument, side, entry_type, entry, stop, target, expires_ts, "
    "verdict, status, r_result, resolved_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
GOOD_PLAN = (
    NOW_TS - 60,
    "binanceusdm",
    "BTC/USDT:USDT",
    "perp",
    "long",
    "market",
    100.0,
    95.0,
    110.0,
    NOW_TS + 86400,
    "enter",
    "open",
    None,
    None,
)


def _plan(**cols) -> tuple:
    names = ["created_ts", "exchange", "symbol", "instrument", "side", "entry_type", "entry", "stop", "target", "expires_ts"]
    names += ["verdict", "status", "r_result", "resolved_ts"]
    row = dict(zip(names, GOOD_PLAN, strict=True)) | {"created_ts": NOW_TS, "symbol": "ETH/USDT:USDT"} | cols
    return tuple(row[n] for n in names)


async def test_plan_list_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "plan_list")
    _store_failed(await _call(build_server(rt)[0], "plan_list"))


@pytest.mark.parametrize("col", ["entry", "created_ts", "r_result"])
@pytest.mark.parametrize("bad", CORRUPT)
async def test_plan_list_skips_a_corrupt_plan(rt, bad, col):
    # Before item 280 float(entry), _iso(created_ts) and r(r_result) raised on the string / blob.
    extra = {"status": "resolved", "resolved_ts": NOW_TS} if col == "r_result" else {}
    await _rows(rt, PLAN_SQL, [GOOD_PLAN, _plan(**{col: bad}, **extra)])
    env = await _call(build_server(rt)[0], "plan_list")
    assert env["ok"] is True and env["data"]["count"] == 1
    p = env["data"]["plans"][0]
    assert p["symbol"] == "BTC/USDT:USDT" and p["rr"] == pytest.approx(2.0) and p["risk_pct"] == pytest.approx(0.05)
    assert _skipped(env, "plan")


@pytest.mark.parametrize("levels", [{"stop": 100.0}, {"entry": 0.0}])
async def test_plan_list_skips_degenerate_levels(rt, levels):
    # entry = stop or entry 0 raised ZeroDivisionError in geometry() before item 280.
    await _rows(rt, PLAN_SQL, [GOOD_PLAN, _plan(**levels)])
    env = await _call(build_server(rt)[0], "plan_list")
    assert env["ok"] is True and env["data"]["count"] == 1
    assert any(w.startswith("1 stored plan rows skipped: entry <= 0") for w in env["warnings"])


# ---------- iv_rank (open finding of item 279) ----------


async def _atm_iv(rt, ivs: list) -> None:
    n = len(ivs)
    await _rows(
        rt,
        "INSERT INTO atm_term VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(NOW_TS - (n - 1 - i) * 3600, "BTC", EXPIRY, 48.0, 83000.0, v, -0.02, 0.01, None, None) for i, v in enumerate(ivs)],
    )


async def test_iv_rank_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "atm_term_series")
    _store_failed(await _call(build_server(rt)[0], "iv_rank", currency="BTC", expiry=EXPIRY))


@pytest.mark.parametrize("bad", CORRUPT)
async def test_iv_rank_skips_a_corrupt_atm_iv(rt, bad):
    # Before item 280 min() / max() over the string / blob raised TypeError; inf gave rank 0 with max_iv Infinity.
    await _atm_iv(rt, [0.40 + 0.01 * i for i in range(11)] + [bad])
    env = await _call(build_server(rt)[0], "iv_rank", currency="BTC", expiry=EXPIRY, lookback_days=1)
    d = env["data"]
    assert env["ok"] is True and d["count"] == 11 and d["max_iv"] == pytest.approx(0.50) and d["iv_rank"] == pytest.approx(1.0)
    assert d["latest"]["ts"] == NOW_TS - 3600 and d["latest"]["atm_iv"] == pytest.approx(0.50)
    assert any("1 stored rows with a non-numeric or non-finite atm_iv skipped" in w for w in env["warnings"])


# ---------- ohlcv_history (open finding of item 279) ----------


async def _bars(rt, closes: list) -> None:
    n = len(closes)
    await _rows(
        rt,
        "INSERT INTO ohlcv_hist VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("binanceusdm", "BTC/USDT:USDT", "1h", (NOW_TS - (n - 1 - i) * 3600) * 1000, 1.0, 2.0, 0.5, c, 10.0)
            for i, c in enumerate(closes)
        ],
    )


OH_ARGS = {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"}


@pytest.mark.parametrize("bad", CORRUPT)
async def test_ohlcv_history_skips_a_corrupt_bar(rt, bad):
    # Before item 280 the cell went out as is (an invalid UTF-8 blob failed the whole reply, inf gave Infinity).
    await _bars(rt, [1.5, 1.6, 1.7, bad])
    env = await _call(build_server(rt)[0], "ohlcv_history", **OH_ARGS)
    d = env["data"]
    assert env["ok"] is True and d["count"] == 3 and [x["close"] for x in d["bars"]] == [1.5, 1.6, 1.7]
    assert d["last_ts_ms"] == (NOW_TS - 3600) * 1000
    assert _skipped(env, "bar")


async def test_ohlcv_history_only_corrupt_bars_is_not_ok(rt):
    await _bars(rt, ["abc", b"\xff"])
    env = await _call(build_server(rt)[0], "ohlcv_history", **OH_ARGS)
    assert env["ok"] is False and env["data"]["count"] == 0 and env["data"]["bars"] == []
    assert any(m["field"] == "ohlcv_hist" and "malformed" in m["detail"] for m in env["missing"])


async def test_ohlcv_history_corrupt_volume_skips_the_bar(rt):
    await _bars(rt, [1.5, 1.6])
    await _rows(
        rt,
        "INSERT INTO ohlcv_hist VALUES (?,?,?,?,?,?,?,?,?)",
        [("binanceusdm", "BTC/USDT:USDT", "1h", (NOW_TS + 3600) * 1000, 1, 2, 0.5, 1.5, "n/a")],
    )
    env = await _call(build_server(rt)[0], "ohlcv_history", **OH_ARGS)
    assert env["ok"] is True and env["data"]["count"] == 2 and _skipped(env, "bar")


async def test_ohlcv_history_valid_bars_unchanged_without_a_warning(rt):
    await _bars(rt, [1.5, 1.6, 1.7])
    env = await _call(build_server(rt)[0], "ohlcv_history", **OH_ARGS)
    assert env["ok"] is True and env["data"]["count"] == 3 and env["warnings"] == []
    assert env["data"]["bars"][0] == {"ts": (NOW_TS - 2 * 3600) * 1000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}
