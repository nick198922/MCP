from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ccxt
import httpx
import pytest
from mcp import Client

from conftest import FakeEx, State, make_transports
from tradedesk.analytics.forecast_log import (
    KINDS,
    MIN_EXPIRIES,
    ONE_SIGMA,
    as_model,
    brier,
    build_forecasts,
    calibration,
    compare,
    event_bounds,
    iso_week,
    outcome,
    pick_expiry,
    prob_between,
    prob_lognormal,
    score,
    weekly,
)
from tradedesk.analytics.options import Smile, black76_price, build_chain, build_smile, group_by_expiry, ncdf
from tradedesk.collector.forecast_log import collect_forecast_log
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIXTURE = Path(__file__).parent / "fixtures" / "deribit_book_7d_2026-10-22.json"
SAMPLE_NOW = datetime(2026, 10, 22, 3, 18, 58, tzinfo=UTC)  # fetch time of the fixture

# ----------------------------------------------------------------------------- analytic reference


def _flat(F: float, T: float, iv: float) -> Smile:
    return Smile(F, T, [F * 0.5, F, F * 1.5], [iv, iv, iv])


def _d2(F: float, K: float, T: float, iv: float) -> float:
    return (math.log(F / K) - iv * iv * T / 2) / (iv * math.sqrt(T))


def test_flat_smile_matches_lognormal():
    F, T, iv, spot = 100_000.0, 7 / 365, 0.5, 99_000.0
    sm = _flat(F, T, iv)
    exp = datetime(2026, 10, 30, 8, tzinfo=UTC)
    rows = {r["kind"]: r for r in build_forecasts(sm, asset="BTC", spot=spot, expiry=exp, now=exp - timedelta(days=7))}
    assert list(rows) == list(KINDS)
    a = iv * math.sqrt(T) / 2
    # +-1 sigma around the forward: N(1 - a) - N(-1 - a), close to 0.6827 for a short horizon
    assert rows["in_1sigma"]["prob"] == pytest.approx(ncdf(1 - a) - ncdf(-1 - a), abs=1e-9)
    assert rows["in_1sigma"]["prob"] == pytest.approx(ONE_SIGMA, abs=0.01)
    assert rows["above_spot"]["prob"] == pytest.approx(ncdf(_d2(F, spot, T, iv)), abs=1e-9)
    p5 = ncdf(_d2(F, spot * 0.95, T, iv)) - ncdf(_d2(F, spot * 1.05, T, iv))
    assert rows["in_pct_5"]["prob"] == pytest.approx(p5, abs=1e-9)
    assert rows["in_pct_5"]["prob"] < rows["in_pct_10"]["prob"] < 1.0
    assert rows["above_spot"]["ref_prob"] == 0.5 and rows["in_1sigma"]["ref_prob"] == pytest.approx(0.682689, abs=1e-6)
    assert rows["in_pct_5"]["ref_prob"] is None
    assert all(r["covered"] for r in rows.values()) and rows["in_1sigma"]["day"] == "2026-10-23"
    assert event_bounds("in_1sigma", spot=spot, forward=F, sigma=iv, t_years=T) == pytest.approx(
        (F * math.exp(-2 * a), F * math.exp(2 * a))
    )
    assert prob_between(sm, None, None) == 1.0
    assert build_forecasts(_flat(F, 0.0, iv), asset="BTC", spot=spot, expiry=exp, now=exp) == []


def test_pick_expiry_band():
    now = datetime(2026, 10, 22, 10, tzinfo=UTC)
    exps = [now + timedelta(days=d) for d in (1, 3.9, 8, 15)]
    assert pick_expiry(exps, now) == exps[2]
    assert pick_expiry([now + timedelta(days=1), now + timedelta(days=12)], now) is None


def test_score_hand_computed():
    rows = [
        {"asset": "BTC", "kind": "above_spot", "expiry_ts": 1, "prob": 0.8, "ref_prob": 0.5, "outcome": 1},
        {"asset": "BTC", "kind": "above_spot", "expiry_ts": 2, "prob": 0.3, "ref_prob": 0.5, "outcome": 0},
        {"asset": "BTC", "kind": "in_1sigma", "expiry_ts": 2, "prob": 0.7, "ref_prob": ONE_SIGMA, "outcome": 0},
        {"asset": "BTC", "kind": "in_pct_5", "expiry_ts": 3, "prob": 0.5, "ref_prob": None, "outcome": None},  # open: ignored
    ]
    s = score(rows)
    bs = (0.04 + 0.09 + 0.49) / 3
    assert s["n"] == 3 and s["n_expiries"] == 2
    assert s["brier"] == pytest.approx(bs)
    # in-sample climatology: base rate 0.5 for above_spot, 0 for in_1sigma
    assert s["brier_climatology"] == pytest.approx((0.25 + 0.25 + 0.0) / 3)
    ref = (0.25 + 0.25 + ONE_SIGMA**2) / 3
    assert s["brier_ref"] == pytest.approx(ref) and s["skill_vs_ref"] == pytest.approx(1 - bs / ref)
    assert [b["lo"] for b in s["calibration"]] == pytest.approx([0.3, 0.7, 0.8])
    # one forecast per bin: the binned Murphy decomposition is exact
    assert s["reliability"] - s["resolution"] + s["uncertainty"] == pytest.approx(bs)
    assert s["uncertainty"] == pytest.approx(1 / 3 * 2 / 3)
    assert score([]) == {"n": 0}
    assert outcome(None, 10.0, 9.0) == 1 and outcome(5.0, None, 5.0) == 0 and outcome(5.0, 10.0, 10.0) == 0
    assert brier(0.25, 1) == pytest.approx(0.5625)


def test_calibration_and_weekly():
    wk1 = int(datetime(2026, 10, 30, 8, tzinfo=UTC).timestamp())  # Friday of ISO week 44
    wk2 = int(datetime(2026, 11, 6, 8, tzinfo=UTC).timestamp())
    rows = [{"asset": "BTC", "kind": "in_pct_10", "expiry_ts": wk1, "prob": 0.9, "ref_prob": None, "outcome": o} for o in (1, 1, 1, 0)]
    rows += [{"asset": "ETH", "kind": "above_spot", "expiry_ts": wk2, "prob": 1.0, "ref_prob": 0.5, "outcome": 1}]
    bins = calibration(rows)
    assert [(b["lo"], b["n"]) for b in bins] == [(0.9, 5)]  # 1.0 falls into the last bin
    assert bins[0]["freq"] == pytest.approx(0.8) and bins[0]["mean_prob"] == pytest.approx(0.92)
    w = weekly(rows)
    assert [x["week"] for x in w] == [iso_week(wk1), iso_week(wk2)] == ["2026-W44", "2026-W45"]
    assert w[0]["n"] == 4 and w[0]["brier"] == pytest.approx((3 * 0.01 + 0.81) / 4) and w[0]["n_ref"] == 0
    assert w[1]["brier_by_kind"] == {"above_spot": 0.0} and w[1]["skill_vs_ref"] == pytest.approx(1.0)


def test_rv30_model_lognormal_reference():
    spot, sig, T = 80_000.0, 0.4, 7 / 365
    w = sig * math.sqrt(T)
    assert prob_lognormal(spot * math.exp(-w), spot * math.exp(w), spot=spot, sigma=sig, t_years=T) == pytest.approx(ONE_SIGMA, abs=1e-12)
    assert prob_lognormal(spot, None, spot=spot, sigma=sig, t_years=T) == pytest.approx(0.5)  # zero median drift
    assert prob_lognormal(None, None, spot=spot, sigma=sig, t_years=T) == 1.0
    p5 = ncdf(math.log(1.05) / w) - ncdf(math.log(0.95) / w)
    assert prob_lognormal(spot * 0.95, spot * 1.05, spot=spot, sigma=sig, t_years=T) == pytest.approx(p5, abs=1e-12)
    with pytest.raises(ValueError):
        prob_lognormal(1.0, 2.0, spot=spot, sigma=0.0, t_years=T)

    F, iv = 80_400.0, 0.5
    exp = datetime(2026, 10, 30, 8, tzinfo=UTC)
    now = exp - timedelta(days=7)
    rows = {r["kind"]: r for r in build_forecasts(_flat(F, T, iv), asset="BTC", spot=spot, expiry=exp, now=now, rv=sig)}
    assert rows["above_spot"]["prob_rv"] == pytest.approx(0.5) and rows["in_pct_5"]["prob_rv"] == pytest.approx(p5, abs=1e-12)
    lo, hi = rows["in_1sigma"]["lo"], rows["in_1sigma"]["hi"]  # the options' band around F at the ATM IV, priced by RV30
    assert rows["in_1sigma"]["prob_rv"] == pytest.approx(ncdf(math.log(hi / spot) / w) - ncdf(math.log(lo / spot) / w), abs=1e-12)
    assert all(r["rv30"] == sig for r in rows.values())
    bare = build_forecasts(_flat(F, T, iv), asset="BTC", spot=spot, expiry=exp, now=now)
    assert all(r["rv30"] is None and r["prob_rv"] is None for r in bare)


def test_compare_hand_computed():
    rows = [
        # expiry 1: the options better on both rows
        {"asset": "BTC", "kind": "above_spot", "expiry_ts": 1, "prob": 0.6, "prob_rv": 0.5, "outcome": 1},
        {"asset": "BTC", "kind": "in_pct_5", "expiry_ts": 1, "prob": 0.9, "prob_rv": 0.7, "outcome": 1},
        # expiry 2: RV30 better
        {"asset": "BTC", "kind": "in_pct_5", "expiry_ts": 2, "prob": 0.9, "prob_rv": 0.7, "outcome": 0},
        {"asset": "ETH", "kind": "in_pct_5", "expiry_ts": 2, "prob": 0.8, "prob_rv": None, "outcome": 0},  # no RV30: left out
        {"asset": "BTC", "kind": "in_pct_10", "expiry_ts": 3, "prob": 0.9, "prob_rv": 0.8, "outcome": None},  # open: left out
    ]
    d1 = ((0.16 - 0.25) + (0.01 - 0.09)) / 2
    d2 = 0.81 - 0.49
    c = compare(rows)
    assert c["n"] == 3 and c["n_expiries"] == 2 and c["diff"] == pytest.approx((d1 + d2) / 2)
    assert c["se"] == pytest.approx(abs(d1 - d2) / 2)  # sd of two means / sqrt(2)
    assert c["ci95"] == pytest.approx([c["diff"] - 1.96 * c["se"], c["diff"] + 1.96 * c["se"]])
    assert c["brier_risk_neutral"] == pytest.approx((0.16 + 0.01 + 0.81) / 3) and c["brier_rv30"] == pytest.approx((0.25 + 0.09 + 0.49) / 3)
    assert c["better"] == "rv30" and c["enough"] is False and MIN_EXPIRIES == 30
    one = compare(rows[:2])
    assert one["n_expiries"] == 1 and one["se"] is None and one["ci95"] is None and one["better"] == "risk_neutral"
    assert compare([]) == {"n": 0, "n_expiries": 0}
    rv = as_model(rows, "rv30")
    assert [x["prob"] for x in rv] == [0.5, 0.7, 0.7, 0.8] and score(rv)["n"] == 3
    assert as_model(rows, "risk_neutral")[0]["prob"] == 0.6
    with pytest.raises(ValueError):
        as_model(rows, "gut")


# ----------------------------------------------------------------------------- real sample (Deribit, 2026-10-22)


@pytest.mark.parametrize("asset", ["BTC", "ETH"])
def test_real_sample(asset):
    book = json.loads(FIXTURE.read_text())[asset]
    by = group_by_expiry(build_chain(book, SAMPLE_NOW))
    exp = pick_expiry(by, SAMPLE_NOW)
    assert exp == datetime(2026, 10, 30, 8, tzinfo=UTC)
    rows = by[exp]
    sm = build_smile(rows)
    spot = next(x.index for x in rows if x.index)
    out = {r["kind"]: r for r in build_forecasts(sm, asset=asset, spot=spot, expiry=exp, now=SAMPLE_NOW)}
    assert all(r["covered"] for r in out.values())
    assert 0.45 < out["above_spot"]["prob"] < 0.6
    assert 0.6 < out["in_1sigma"]["prob"] < 0.8
    assert out["in_pct_5"]["prob"] < out["in_pct_10"]["prob"] < 1.0
    # independent check of the smile digital: -dC/dK by finite differences of Black-76 calls priced at the smile IV
    F, T = sm.forward, sm.T

    def call(k: float) -> float:
        return black76_price(F, k, T, sm.iv(k), True)

    for kind in ("in_pct_5", "in_pct_10"):
        lo, hi = out[kind]["lo"], out[kind]["hi"]
        digital = [-(call(k * 1.002) - call(k * 0.998)) / (k * 0.004) for k in (lo, hi)]
        assert out[kind]["prob"] == pytest.approx(digital[0] - digital[1], abs=2e-3)


# ----------------------------------------------------------------------------- job and tool

DAY1 = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)  # 30OCT26 08:00 is 6.9 days away (the fake book has 30OCT, 27NOV, 26MAR)
DELIVERY = "2026-10-30"


class DailyEx(FakeEx):
    """FakeEx whose daily bars end on the day of DAY1 (FakeEx's own end on 10.10, stale for the RV30 model):
    closes alternate x1.015 and /1.015, so every |log return| is ln 1.015 and RV30 = ln(1.015) sqrt(365)."""

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
        if timeframe != "1d":
            return await super().fetch_ohlcv(symbol, timeframe, limit, since, params)
        if self.id == "binance" and getattr(DailyEx, "down", False):
            raise ccxt.NetworkError("binance down")
        end = int(DAY1.replace(hour=0).timestamp() * 1000)
        price, rows = 80_000.0, []
        for i in range(limit):
            price *= 1.015 if i % 2 else 1 / 1.015
            rows.append([end - (limit - 1 - i) * 86_400_000, price, price, price, price, 10.0])
        return rows


RV_EXPECTED = math.log(1.015) * math.sqrt(365)


def _rt(tmp_path, *, status: int = 200, delivery: float | None = 83_000.0, ex=FakeEx, **kw) -> tuple[Runtime, State]:
    state = State()
    state.deribit_status = status
    tr = make_transports(state)
    base = tr["deribit"].handler

    def deribit(req: httpx.Request) -> httpx.Response:
        if state.deribit_status == 200 and req.url.path.endswith("get_delivery_prices"):
            rows = [] if delivery is None else [{"date": DELIVERY, "delivery_price": delivery}]
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"data": rows, "records_total": len(rows)}})
        resp = base(req)
        if req.url.params.get("currency") == "ETH" and resp.status_code == 200:
            body = json.loads(resp.content)
            for x in body.get("result") or []:
                x["instrument_name"] = x["instrument_name"].replace("BTC-", "ETH-")
            return httpx.Response(200, json=body)
        return resp

    tr["deribit"] = httpx.MockTransport(deribit)
    s = Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "fc.sqlite"), **kw)
    rt = Runtime(s, transports=tr, exchange_factory=ex)
    rt.now = lambda: DAY1
    return rt, state


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_job_records_once_a_day_then_tool_settles(tmp_path):
    rt, state = _rt(tmp_path)
    try:
        rt.now = lambda: DAY1.replace(hour=8)  # before 09:00 UTC: nothing is recorded
        assert await collect_forecast_log(rt) == 0
        rt.now = lambda: DAY1
        assert await collect_forecast_log(rt) == 2 * len(KINDS)
        calls = len(state.calls)
        assert await collect_forecast_log(rt) == 0  # same day: idempotent, and no network call for the book
        assert len(state.calls) == calls
        rows = await rt.store.forecast_list()
        assert len(rows) == 8 and {r["asset"] for r in rows} == {"BTC", "ETH"} and all(r["status"] == "open" for r in rows)
        assert {r["expiry_ts"] for r in rows} == {int(datetime(2026, 10, 30, 8, tzinfo=UTC).timestamp())}
        assert await rt.store.forecast_insert_many(rows) == 0  # the UNIQUE (day, asset, kind) key holds

        srv, _ = build_server(rt)
        out = await _call(srv, "forecast_log_status")
        assert out["ok"] and out["data"]["n_open"] == 8 and out["data"]["scores"]["all"] == {"n": 0}
        assert [r["kind"] for r in out["data"]["latest"]["BTC"]] == list(KINDS)
        assert any("RISK-NEUTRAL" in w for w in out["warnings"])

        rt.now = lambda: datetime(2026, 10, 31, 12, tzinfo=UTC)
        out = await _call(srv, "forecast_log_status", include_settled=True)
        d = out["data"]
        assert out["ok"] and d["settled_this_call"] == 8 and d["n_open"] == 0 and d["n_settled"] == 8
        assert d["scores"]["all"]["n"] == 8 and d["scores"]["all"]["n_expiries"] == 2
        assert d["weekly"][0]["week"] == "2026-W44" and d["weekly"][0]["n"] == 8
        btc = {r["kind"]: r for r in d["settled"] if r["asset"] == "BTC"}
        assert btc["above_spot"]["outcome"] == 1  # 83 000 > the 82 750 index at issue
        assert btc["in_pct_5"]["outcome"] == 1 and btc["in_pct_10"]["outcome"] == 1
        p = btc["above_spot"]["prob"]
        assert btc["above_spot"]["brier"] == pytest.approx((p - 1) ** 2, abs=1e-4)
        assert d["scores"]["by_kind"]["above_spot"]["brier"] == pytest.approx(
            sum((r["prob"] - r["outcome"]) ** 2 for r in d["settled"] if r["kind"] == "above_spot") / 2, abs=1e-3
        )
    finally:
        await rt.aclose()


async def test_tool_delivery_feed_down_keeps_rows_open(tmp_path):
    rt, state = _rt(tmp_path)
    try:
        assert await collect_forecast_log(rt) == 8
        rt.now = lambda: datetime(2026, 10, 31, 12, tzinfo=UTC)
        state.deribit_status = 500
        srv, _ = build_server(rt)
        out = await _call(srv, "forecast_log_status")
        assert out["ok"] and out["data"]["n_open"] == 8 and out["data"]["settled_this_call"] == 0
        assert {m["field"] for m in out["missing"]} == {"delivery_prices:BTC", "delivery_prices:ETH"}
    finally:
        await rt.aclose()


async def test_tool_delivery_not_published_yet(tmp_path):
    rt, _ = _rt(tmp_path, delivery=None)
    try:
        await collect_forecast_log(rt)
        rt.now = lambda: datetime(2026, 10, 30, 8, 5, tzinfo=UTC)
        srv, _ = build_server(rt)
        out = await _call(srv, "forecast_log_status", asset="ETH")
        assert out["ok"] and out["data"]["n_open"] == 4 and list(out["data"]["latest"]) == ["ETH"]
        assert "forecast_log:ETH:2026-10-30" in {m["field"] for m in out["missing"]}
    finally:
        await rt.aclose()


async def test_job_raises_when_the_book_fails(tmp_path):
    rt, _ = _rt(tmp_path, status=500)
    try:
        with pytest.raises(RuntimeError, match="no data, will retry"):
            await collect_forecast_log(rt)
        assert await rt.store.forecast_list() == []
    finally:
        await rt.aclose()


async def test_tool_without_store():
    rt = Runtime(Settings(db_path=""), transports=make_transports(State()), exchange_factory=FakeEx)
    srv, _ = build_server(rt)
    out = await _call(srv, "forecast_log_status")
    assert out["ok"] is False and out["missing"][0]["field"] == "store"


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    assert Settings().forecast_log_enabled is False
    assert allowed("TRADEDESK_PAPER_FORECAST_LOG_ENABLED") and allowed("TRADEDESK_PAPER_FORECAST_LOG_SECONDS")
    monkeypatch.setenv("TRADEDESK_PAPER_FORECAST_LOG_ENABLED", "1")
    assert Settings.from_env().forecast_log_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, forecast_log_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "forecast_log" not in names(off) and "forecast_log" in names(on)


async def test_rv30_model_recorded_settled_and_compared(tmp_path):
    rt, _ = _rt(tmp_path, ex=DailyEx)
    DailyEx.down = False
    try:
        assert await collect_forecast_log(rt) == 8
        rows = await rt.store.forecast_list()
        assert all(r["rv30"] == pytest.approx(RV_EXPECTED) and 0 < r["prob_rv"] < 1 for r in rows)
        assert all(r["prob_rv"] == pytest.approx(0.5) for r in rows if r["kind"] == "above_spot")
        rt.now = lambda: datetime(2026, 10, 31, 12, tzinfo=UTC)
        srv, _ = build_server(rt)
        out = await _call(srv, "forecast_log_status", include_settled=True)
        d = out["data"]
        assert out["ok"] and d["n_settled"] == 8
        assert all(r["brier_rv"] == pytest.approx((r["prob_rv"] - r["outcome"]) ** 2, abs=1e-4) for r in d["settled"])
        assert d["scores_rv30"]["all"]["n"] == 8
        c = d["comparison"]["all"]
        assert c["n"] == 8 and c["n_expiries"] == 2 and c["enough"] is False
        assert c["diff"] == pytest.approx(c["brier_risk_neutral"] - c["brier_rv30"], abs=1e-4)  # equal rows per expiry
        assert d["comparison"]["by_kind"]["above_spot"]["n"] == 2
        assert any("RV30 model is a naive physical baseline" in w for w in out["warnings"])
    finally:
        await rt.aclose()


async def test_rv30_closes_down_still_records_the_options_forecast(tmp_path):
    rt, _ = _rt(tmp_path, ex=DailyEx)
    DailyEx.down = True
    try:
        assert await collect_forecast_log(rt) == 8  # no raise: the risk-neutral rows do not depend on the closes
        rows = await rt.store.forecast_list()
        assert all(r["prob_rv"] is None and r["rv30"] is None and r["prob"] is not None for r in rows)
        rt.now = lambda: datetime(2026, 10, 31, 12, tzinfo=UTC)
        srv, _ = build_server(rt)
        out = await _call(srv, "forecast_log_status")
        assert out["ok"] and out["data"]["comparison"]["all"] == {"n": 0, "n_expiries": 0}
        assert out["data"]["scores_rv30"]["all"] == {"n": 0} and out["data"]["scores"]["all"]["n"] == 8
    finally:
        DailyEx.down = False
        await rt.aclose()
