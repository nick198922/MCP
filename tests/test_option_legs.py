"""Weekly Deribit condor legs: leg picking (analytics/option_legs.py) and the daily collector (collector/option_legs.py)."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from conftest import State, make_transports
from tradedesk.analytics.option_legs import (
    ROLES,
    entry_day,
    expiry_surface,
    hold_legs,
    leg_row,
    plan_legs,
    snapshot_day,
    weekly_expiry,
)
from tradedesk.analytics.options import black76_greeks
from tradedesk.analytics.put_skew import iv_at_strike
from tradedesk.collector.option_legs import collect_option_legs, scan_option_legs
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store

FIX = Path(__file__).parent / "fixtures"
# Real book summary rows of the 30OCT26 weekly (fetched 2026-10-22 03:19 UTC); the tests read it as of Friday 23.10 09:30.
BOOK = json.loads((FIX / "deribit_book_7d_2026-10-22.json").read_text())
XV = json.loads((FIX / "option_xvenue_btc_2026-10-23.json").read_text())  # real public/ticker results (trimmed)
NOW = datetime(2026, 10, 23, 9, 30, tzinfo=UTC)
EXP = datetime(2026, 10, 30, 8, tzinfo=UTC)
DAY = "2026-10-23"


# ----------------------------------------------------------------------------- pure


def test_snapshot_day_from_nine_utc():
    assert snapshot_day(datetime(2026, 10, 23, 8, 59, tzinfo=UTC)) is None
    assert snapshot_day(datetime(2026, 10, 23, 9, 0, tzinfo=UTC)) == DAY
    assert snapshot_day(datetime(2026, 10, 23, 23, 59, tzinfo=UTC)) == DAY


def test_weekly_expiry_is_the_next_friday_eight_utc():
    assert weekly_expiry(datetime(2026, 10, 23, 9, tzinfo=UTC)) == EXP  # Friday after 08:00: next week (H1 entry)
    assert weekly_expiry(datetime(2026, 10, 23, 7, 59, tzinfo=UTC)) == datetime(2026, 10, 23, 8, tzinfo=UTC)
    assert weekly_expiry(datetime(2026, 10, 24, 9, tzinfo=UTC)) == EXP  # Saturday
    assert weekly_expiry(datetime(2026, 10, 29, 9, tzinfo=UTC)) == EXP  # Thursday: tomorrow


def test_legs_on_the_real_weekly_book():
    for cur in ("BTC", "ETH"):
        x = expiry_surface(BOOK[cur], cur, EXP, NOW)
        assert x is not None and x["T"] == pytest.approx(6.9375 / 365)
        assert x["F"] == pytest.approx({"BTC": 82880.17, "ETH": 2440.61}[cur], rel=1e-5)
        assert x["spot"] == {"BTC": 82753.59, "ETH": 2437.77}[cur]
        legs, lost = plan_legs(x)
        assert not lost and [g["role"] for g in legs] == list(ROLES)
        g = {leg["role"]: leg for leg in legs}
        S, F = x["spot"], x["F"]
        for role, (delta, is_call) in {"sp10": (-0.10, False), "sc10": (0.10, True), "sp25": (-0.25, False), "sc25": (0.25, True)}.items():
            t = g[role]["target"]  # the target strike has the registered delta on the mark smile
            assert black76_greeks(F, t, x["T"], iv_at_strike(x, t)[0], is_call)["delta"] == pytest.approx(delta, abs=1e-6)
        listed = sorted({k for k, _ in x["names"]})
        for leg in legs:  # each leg is the listed strike nearest its target
            assert leg["strike"] == min(listed, key=lambda k, t=leg["target"]: (abs(k - t), k))
            assert leg["instrument"].endswith(f"-{leg['strike']:g}-{leg['opt']}")
        assert g["atm_p"]["strike"] == g["atm_c"]["strike"] == min(listed, key=lambda k: abs(k - F))
        assert g["lp15"]["target"] == pytest.approx(0.85 * S) and g["lc15"]["target"] == pytest.approx(1.15 * S)
        assert g["lp"]["target"] == pytest.approx(min(0.85 * S, g["sp10"]["strike"] - 0.05 * S))
        assert g["lc"]["target"] == pytest.approx(max(1.15 * S, g["sc10"]["strike"] + 0.05 * S))
        assert g["lp"]["floor"] is False and g["lc"]["floor"] is False  # calm week: the +-15 % level sets the wings
        ks = [g[r]["strike"] for r in ("lp", "sp10", "sp25", "atm_p", "sc25", "sc10", "lc")]
        assert ks == sorted(ks) and len(set(ks)) == 7


def _flat(sigma: float, days: float = 7.0) -> dict:
    ks = [float(k) for k in range(40, 161)]
    names = {(k, t): f"BTC-30OCT26-{k:g}-{t}" for k in ks for t in "CP"}
    return {"F": 100.0, "T": days / 365, "spot": 100.0, "strikes": {k: {"mark_iv": sigma} for k in ks}, "names": names, "rows": {}}


def test_ten_delta_strike_and_width_floor_on_a_flat_smile():
    # Flat 150 % vol, 7 days: put delta -0.10 <=> N(d1) = 0.9, d1 = 1.281552; ln(F/K) = d1 s sqrt(T) - s^2 T / 2.
    s, T = 1.5, 7 / 365
    k_star = 100.0 * math.exp(-(1.2815516 * s * math.sqrt(T) - 0.5 * s * s * T))
    legs, lost = plan_legs(_flat(s))
    g = {leg["role"]: leg for leg in legs}
    assert not lost and g["sp10"]["target"] == pytest.approx(k_star, rel=1e-5) and g["sp10"]["strike"] == round(k_star)
    assert g["lp"]["floor"] is True and g["lp"]["target"] == pytest.approx(g["sp10"]["strike"] - 5.0)  # w/3 = 5 % of spot
    assert g["lp15"]["target"] == pytest.approx(85.0) and g["lp"]["strike"] < g["lp15"]["strike"]


def test_leg_row_from_a_real_ticker_and_from_the_summary():
    x = expiry_surface(BOOK["BTC"], "BTC", EXP, NOW)
    leg = {"role": "atm_c", "opt": "C", "target": x["F"], "strike": 83000.0, "instrument": "BTC-30OCT26-83000-C", "floor": None}
    t = XV["deribit_ticker"]["BTC-23OCT26-83000-C"]  # real ticker: no bid (0 -> NULL), ask 0.0001-scale prices
    r = leg_row(leg, x, t, currency="BTC", expiry=EXP, now=NOW)
    assert r["src"] == "ticker" and r["bid"] is None and r["bid_iv"] is None and r["ask"] == t["best_ask_price"]
    assert r["mark_iv"] == pytest.approx(t["mark_iv"] / 100) and r["quote_ts_ms"] == t["timestamp"] and r["mark"] is None
    s = leg_row(leg, x, None, currency="BTC", expiry=EXP, now=NOW)
    row = next(b for b in BOOK["BTC"] if b["instrument_name"] == "BTC-30OCT26-83000-C")
    assert s["src"] == "summary" and (s["bid"], s["ask"], s["mark"]) == (row["bid_price"], row["ask_price"], row["mark_price"])
    assert s["mark_iv"] == pytest.approx(row["mark_iv"] / 100) and s["bid_size"] is None and s["open_interest"] == row["open_interest"]
    assert s["tau_days"] == pytest.approx(6.9375) and s["dist"] == pytest.approx((83000 - x["F"]) / x["spot"])
    assert 0.45 < s["delta"] < 0.55  # ATM call


def test_missing_expiry_or_empty_answer_gives_no_surface():
    assert expiry_surface(BOOK["BTC"], "BTC", EXP + timedelta(days=7), NOW) is None
    assert expiry_surface(BOOK["BTC"], "ETH", EXP, NOW) is None
    assert expiry_surface({"bad": 1}, "BTC", EXP, NOW) is None
    assert expiry_surface(BOOK["BTC"], "BTC", EXP, EXP) is None  # T = 0


# ----------------------------------------------------------------------------- collector


def _ticker(row: dict) -> dict:
    """A public/ticker result built from a real summary row; bid/ask IV are mark +- 1 vol point (synthetic)."""
    return {
        "instrument_name": row["instrument_name"],
        "timestamp": row["creation_timestamp"],
        "best_bid_price": row["bid_price"] or 0.0,
        "best_ask_price": row["ask_price"] or 0.0,
        "best_bid_amount": 1.5 if row["bid_price"] else 0.0,
        "best_ask_amount": 2.5,
        "mark_price": row["mark_price"],
        "mark_iv": row["mark_iv"],
        "bid_iv": row["mark_iv"] - 1.0 if row["bid_price"] else 0.0,
        "ask_iv": row["mark_iv"] + 1.0,
        "open_interest": row["open_interest"],
        "stats": {"volume": row["volume"]},
    }


def _rt(tmp_path, opts: dict | None = None, **kw) -> tuple[Runtime, list]:
    """Fake Deribit. ``opts`` (read on every request): "down" (all 500), "down_cur" (one currency's summary 500),
    "bad_ticker" (instrument names answering instrument_not_found)."""
    calls: list = []
    opts = opts if opts is not None else {}
    by_name = {r["instrument_name"]: r for cur in ("BTC", "ETH") for r in BOOK[cur]}

    def deribit(req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[-1]
        arg = req.url.params.get("instrument_name") or req.url.params.get("currency")
        calls.append((method, arg))
        if opts.get("down") or (method == "get_book_summary_by_currency" and arg == opts.get("down_cur")):
            return httpx.Response(500, text="down")
        if method == "get_book_summary_by_currency":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": BOOK[arg]})
        if method == "ticker" and arg in by_name and arg not in opts.get("bad_ticker", ()):
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": _ticker(by_name[arg])})
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": 13020, "message": "instrument_not_found"}})

    tr = make_transports(State())
    tr["deribit"] = httpx.MockTransport(deribit)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite"), **kw), transports=tr)
    rt.now = lambda: NOW
    rt.deribit.http._limiter = TokenBucket(1_000.0, 1_000)
    rt.deribit.http._backoff = 0.0
    return rt, calls


async def test_job_stores_ten_legs_per_currency_once_a_day(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_option_legs(rt) == 20
        rows = await rt.store.option_leg_rows()
        assert {(r["day"], r["currency"]) for r in rows} == {(DAY, "BTC"), (DAY, "ETH")}
        assert sorted(r["role"] for r in rows if r["currency"] == "BTC") == sorted(ROLES)
        assert all(r["src"] == "ticker" and r["ts"] == int(NOW.timestamp()) and r["expiry_ts"] == int(EXP.timestamp()) for r in rows)
        sp = next(r for r in rows if r["currency"] == "BTC" and r["role"] == "sp10")
        assert (sp["instrument"], sp["bid"], sp["ask"], sp["bid_size"], sp["ask_size"]) == ("BTC-30OCT26-78000-P", 0.0035, 0.0038, 1.5, 2.5)
        assert sp["mark_iv"] == pytest.approx(0.3674) and sp["bid_iv"] == pytest.approx(0.3574) and sp["ask_iv"] == pytest.approx(0.3774)
        assert -0.13 < sp["delta"] < -0.09 and sp["spot"] == 82753.59 and sp["tau_days"] == pytest.approx(6.9375)
        assert abs(sp["dist"]) <= 0.015 and sp["floor"] is None
        lp = next(r for r in rows if r["currency"] == "BTC" and r["role"] == "lp")
        assert lp["floor"] == 0 and lp["instrument"] == "BTC-30OCT26-70000-P"
        tickers = [a for m, a in calls if m == "ticker"]
        assert len(tickers) == len(set(tickers)) == 16  # lp = lp15 and lc = lc15 here: one read per distinct instrument
        n = len(calls)
        assert await collect_option_legs(rt) == 0 and len(calls) == n  # the day is stored: no request at all
        later = NOW + timedelta(days=1)
        rt.now = lambda: later
        assert await collect_option_legs(rt) == 40  # Saturday: a new day, the same weekly expiry, re-picked + held legs
        assert {r["expiry_ts"] for r in await rt.store.option_leg_rows("2026-10-24")} == {int(EXP.timestamp())}
    finally:
        await rt.aclose()


async def test_saturday_to_thursday_also_quotes_the_friday_entry_legs(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_option_legs(rt) == 20  # Friday entry
        entry = {(r["currency"], r["role"]): r for r in await rt.store.option_leg_rows(DAY)}
        assert not any(role.startswith("hold_") for _, role in entry)
        for d, n in ((1, 40), (6, 40)):  # Saturday and Thursday: 10 re-picked + 10 held legs per currency
            now = NOW + timedelta(days=d)
            rt.now = lambda now=now: now
            before = len(calls)
            assert await collect_option_legs(rt) == n
            day = now.strftime("%Y-%m-%d")
            rows = [r for r in await rt.store.option_leg_rows(day) if r["day"] == day]
            held = [r for r in rows if r["role"].startswith("hold_")]
            assert len(held) == 20 and {r["currency"] for r in held} == {"BTC", "ETH"}
            for r in held:
                e = entry[(r["currency"], r["role"].removeprefix("hold_"))]
                assert (r["instrument"], r["strike"], r["target"], r["floor"], r["opt"]) == (
                    e["instrument"], e["strike"], e["target"], e["floor"], e["opt"]
                )  # fmt: skip
                assert r["src"] == "ticker" and r["expiry_ts"] == e["expiry_ts"] and r["ts"] == int(now.timestamp())
                assert r["tau_days"] == pytest.approx(6.9375 - d) and r["mark"] is not None
            assert sorted(r["role"] for r in rows if not r["role"].startswith("hold_")) == sorted(ROLES * 2)
            tickers = [a for m, a in calls[before:] if m == "ticker"]
            names = {r["instrument"] for r in rows}
            assert len(tickers) == len(set(tickers)) and set(tickers) == names  # one read per distinct instrument
    finally:
        await rt.aclose()


async def test_no_friday_entry_skips_the_held_legs_with_a_logged_warning(tmp_path, caplog):
    rt, calls = _rt(tmp_path)
    sat = NOW + timedelta(days=1)
    rt.now = lambda: sat  # the collector was down on Friday: nothing to hold
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_option_legs(rt) == 20
        rows = await rt.store.option_leg_rows()
        assert {r["day"] for r in rows} == {"2026-10-24"} and not any(r["role"].startswith("hold_") for r in rows)
        logged = [r.getMessage() for r in caplog.records if "hold_ legs skipped" in r.getMessage()]
        assert len(logged) == 2 and all(m.startswith("option_legs 2026-10-24: ") for m in logged)
    finally:
        await rt.aclose()


def test_entry_day_and_hold_legs():
    assert entry_day(NOW) is None  # Friday: the new week's entry, last week's condor expired at 08:00
    assert entry_day(NOW + timedelta(days=1)) == entry_day(NOW + timedelta(days=6, hours=14)) == DAY
    legs, _ = plan_legs(expiry_surface(BOOK["BTC"], "BTC", EXP, NOW))
    stored = [{**g, "floor": None if g["floor"] is None else int(g["floor"]), "expiry_ts": int(EXP.timestamp())} for g in legs]
    other = {**stored[0], "expiry_ts": int((EXP - timedelta(days=7)).timestamp())}  # another expiry
    held = hold_legs([other, *stored[1:], {**stored[2], "role": "hold_sp10"}], EXP)
    assert [g["role"] for g in held] == [f"hold_{r}" for r in ROLES[1:]]
    assert [g["instrument"] for g in held] == [g["instrument"] for g in legs[1:]]
    assert next(g for g in held if g["role"] == "hold_lp")["floor"] is False
    assert hold_legs(stored, EXP + timedelta(days=7)) == [] and hold_legs([], EXP) == []


async def test_before_nine_utc_makes_no_request(tmp_path):
    rt, calls = _rt(tmp_path)
    rt.now = lambda: datetime(2026, 10, 23, 8, 59, tzinfo=UTC)
    try:
        assert await collect_option_legs(rt) == 0 and calls == []
    finally:
        await rt.aclose()


async def test_deribit_down_never_raises_from_the_scan_job_fails_and_retries(tmp_path):
    opts = {"down": True}
    rt, _ = _rt(tmp_path, opts)
    try:
        b = Builder()
        assert await scan_option_legs(rt, b, "BTC", NOW) == []
        assert [(m.field, m.reason) for m in b.missing] == [("deribit_options:BTC", "backend_error")]
        with pytest.raises(RuntimeError, match="option_legs 2026-10-23: no rows"):  # the scheduler records a failed run
            await collect_option_legs(rt)
        assert await rt.store.option_leg_rows() == []
        opts["down"] = False
        assert await collect_option_legs(rt) == 20  # the next tick heals the day
    finally:
        await rt.aclose()


async def test_one_currency_down_keeps_the_other_and_retries_it(tmp_path):
    opts = {"down_cur": "ETH"}
    rt, _ = _rt(tmp_path, opts)
    try:
        assert await collect_option_legs(rt) == 10  # BTC written, ETH missing: logged, not raised
        assert {r["currency"] for r in await rt.store.option_leg_rows()} == {"BTC"}
        opts["down_cur"] = None
        assert await collect_option_legs(rt) == 10  # only ETH is fetched again
        assert {r["currency"] for r in await rt.store.option_leg_rows()} == {"BTC", "ETH"}
    finally:
        await rt.aclose()


async def test_failed_ticker_falls_back_to_the_summary_quote(tmp_path):
    rt, _ = _rt(tmp_path, {"bad_ticker": {"BTC-30OCT26-78000-P"}})
    try:
        b = Builder()
        rows = await scan_option_legs(rt, b, "BTC", NOW)
        sp = next(r for r in rows if r["role"] == "sp10")
        assert sp["src"] == "summary" and (sp["bid"], sp["ask"], sp["mark"]) == (0.0035, 0.0038, 0.00364626) and sp["bid_size"] is None
        assert [(m.field, m.reason) for m in b.missing] == [("deribit_ticker:BTC-30OCT26-78000-P", "data_unavailable")]
        assert sum(r["src"] == "ticker" for r in rows) == 9
    finally:
        await rt.aclose()


async def test_weekly_expiry_absent_is_missing_not_a_crash(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        b = Builder()
        assert await scan_option_legs(rt, b, "BTC", NOW + timedelta(days=7)) == []  # the book has no 06NOV26
        assert [(m.field, m.reason) for m in b.missing] == [("weekly_expiry:BTC", "data_unavailable")]
        assert not [c for c in calls if c[0] == "ticker"]
    finally:
        await rt.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    assert Settings().collect_option_legs_enabled is False and Settings().collect_option_legs_seconds == 600
    assert allowed("TRADEDESK_COLLECT_OPTION_LEGS_ENABLED") and allowed("TRADEDESK_COLLECT_OPTION_LEGS_SECONDS")
    monkeypatch.setenv("TRADEDESK_COLLECT_OPTION_LEGS_ENABLED", "1")
    assert Settings.from_env().collect_option_legs_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_option_legs_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "option_legs" not in names(off) and "option_legs" in names(on)


async def test_migration_creates_the_table(tmp_path):
    s = Store(str(tmp_path / "m.sqlite"))
    try:
        await s.init()
        assert await s.version() == SCHEMA_VERSION >= 36
        assert await s.insert_option_legs(DAY, []) == 0 and not await s.option_legs_has_day(DAY, "BTC")
    finally:
        await s.aclose()


async def test_insert_twice_keeps_the_first_snapshot(tmp_path):
    s = Store(str(tmp_path / "m.sqlite"))
    try:
        await s.init()
        x = expiry_surface(BOOK["BTC"], "BTC", EXP, NOW)
        leg = next(g for g in plan_legs(x)[0] if g["role"] == "sp10")
        row = {**leg_row(leg, x, None, currency="BTC", expiry=EXP, now=NOW), "ts": int(NOW.timestamp())}
        assert row["bid"] == 0.0035
        assert await s.insert_option_legs(DAY, [row]) == 1
        assert await s.insert_option_legs(DAY, [{**row, "bid": 0.0099, "ts": row["ts"] + 600}]) == 0
        (stored,) = await s.option_leg_rows(DAY)
        assert (stored["bid"], stored["ts"]) == (0.0035, row["ts"]) and await s.option_legs_has_day(DAY, "BTC")
    finally:
        await s.aclose()
