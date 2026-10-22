"""Radar archive: pure surface rows (analytics/radar.py), the table radar_surface and the collector job `radar_archive`."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from conftest import FakeEx, State, make_transports
from tradedesk.analytics.macro_calendar import build_macro_calendar
from tradedesk.analytics.options import black76_greeks
from tradedesk.analytics.put_skew import DAY_MS, HOUR_MS, build_day, inverse_price, iv_at_put_delta, iv_at_strike, year_frac
from tradedesk.analytics.radar import (
    LAG_MINUTES,
    METHOD,
    REF_HOUR_UTC,
    WINDOW_HOURS,
    build_surface,
    constant_tenor_iv,
    due_days,
    forward_vols,
    iv_rank,
    normalize_row,
    radar_row,
    radar_view,
    trailing_rv,
    window_ms,
)
from tradedesk.analytics.vrp import realized_vol
from tradedesk.analytics.weekly_straddle import constant_maturity_atm_iv
from tradedesk.collector.radar_archive import collect_radar_archive
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.tools.macro import _load_seed

FIXTURE = Path(__file__).parent / "fixtures" / "deribit_radar_surface_2026-10-18.json"
# archive rows of 11.10 and 18.10, daily DVOL 2025-08-25 .. 2026-10-22, Binance daily closes (fetched 2026-10-22)
INPUTS = Path(__file__).parent / "fixtures" / "radar_snapshot_inputs_2026-10-22.json"
D0 = int(datetime(2026, 10, 18, tzinfo=UTC).timestamp() * 1000)
T_REF = D0 + 19 * HOUR_MS

# ----------------------------------------------------------------------------- analytic reference


def _expiry(days: float, iv_of, F: float = 100_000.0) -> tuple[int, dict]:
    T = days / 365.0
    strikes = {}
    for K in range(50_000, 152_000, 2_000):
        v = iv_of(F, float(K))
        strikes[float(K)] = {"mark_iv": v, "put_delta": black76_greeks(F, float(K), T, v, False)["delta"]}
    return T_REF + int(days * DAY_MS), {"F": F, "T": T, "n_prints": 10, "strikes": strikes}


def _snap(*exps: tuple[int, dict]) -> dict:
    return {"day": D0, "t_ms": T_REF, "spot": 99_500.0, "expiries": dict(exps)}


def test_constant_tenor_total_variance_hand_computed():
    snap = _snap(_expiry(20, lambda F, K: 0.40), _expiry(40, lambda F, K: 0.50))
    atm = lambda x: iv_at_strike(x, x["F"])[0]  # noqa: E731
    # sigma^2 T linear in T: (0.16 * 20 + (0.25 * 40 - 0.16 * 20) * 10 / 20) / 30 = 0.22
    assert constant_tenor_iv(snap, 30, atm) == pytest.approx(math.sqrt(0.22), abs=1e-12)
    assert constant_tenor_iv(snap, 30, atm) == pytest.approx(constant_maturity_atm_iv(snap, 30), abs=1e-12)
    assert constant_tenor_iv(snap, 20, atm) == pytest.approx(0.40)  # on an expiry: its own IV
    assert constant_tenor_iv(snap, 7, atm) is None and constant_tenor_iv(snap, 90, atm) is None  # no extrapolation


def test_flat_smile_has_no_skew():
    row = radar_row(_snap(_expiry(20, lambda F, K: 0.5), _expiry(40, lambda F, K: 0.5)))
    assert row["atm"]["30"] == pytest.approx(0.5) and row["atm"]["7"] is None and row["atm"]["180"] is None
    assert row["put_skew_25"] == pytest.approx(0.0, abs=1e-12) and row["rr_25"] == pytest.approx(0.0, abs=1e-12)
    assert all(v == pytest.approx(0.5) for v in row["smile"].values())
    assert row["day"] == "2026-10-18" and row["ts"] == T_REF // 1000 and row["method"] == METHOD and row["n_prints"] == 20
    assert [e[0] for e in row["expiries"]] == sorted(e[0] for e in row["expiries"]) and row["expiries"][0][1] == pytest.approx(20)


def test_skewed_smile_on_the_tenor():
    """Puts richer than calls: positive put skew, negative RR; on an expiry at exactly 30 d the row equals that expiry's smile."""
    snap = _snap(_expiry(30, lambda F, K: 0.5 + 0.3 * math.log(F / K)))
    x = next(iter(snap["expiries"].values()))
    row = radar_row(snap)
    p25, c25 = iv_at_put_delta(x, -0.25), iv_at_put_delta(x, -0.75)
    assert row["put_skew_25"] == pytest.approx(p25 - 0.5, abs=1e-12) and row["put_skew_25"] > 0.02
    assert row["rr_25"] == pytest.approx(c25 - p25, abs=1e-12) and row["rr_25"] < -0.04
    s = row["smile"]
    assert s["10P"] > s["25P"] > s["35P"] > s["ATM"] > s["35C"] > s["25C"] > s["10C"]


def test_radar_row_empty():
    assert radar_row(None) is None and radar_row({"day": D0, "t_ms": T_REF, "spot": 1.0, "expiries": {}}) is None


def test_due_days_and_window():
    """A day is due only 60 min after its 19:00 UTC reference: the row is frozen on write, history must be complete."""
    before = datetime(2026, 10, 22, 19, 59, tzinfo=UTC)
    after = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    d22 = int(datetime(2026, 10, 22, tzinfo=UTC).timestamp() * 1000)
    assert LAG_MINUTES == 60
    assert due_days(before, 2) == [d22 - DAY_MS, d22 - 2 * DAY_MS]
    assert due_days(after, 2) == [d22, d22 - DAY_MS]
    assert window_ms(d22) == (d22 + 13 * HOUR_MS, d22 + 19 * HOUR_MS)


# ----------------------------------------------------------------------------- real sample


def _load(asset: str) -> dict:
    s = json.loads(FIXTURE.read_text())["surfaces"][asset]
    exps = {}
    for e, x in s["expiries"].items():
        exps[int(e)] = dict(x, strikes={float(k): v for k, v in x["strikes"].items()})
    return dict(s, expiries=exps)


# 2026-10-18 19:00 UTC, the card's «Підсумки 18.10» (rendered by the prototype, 0.1 vol point precision)
CARD = {
    "BTC": {"spot": 81_093, "atm": (0.341, 0.370, 0.371, 0.382), "put_skew": 0.027},
    "ETH": {"spot": 2_363, "atm": (0.520, None, None, None), "put_skew": 0.040},
}


@pytest.mark.parametrize("asset", ["BTC", "ETH"])
def test_real_sample_matches_the_card(asset):
    row = radar_row(_load(asset))
    ref = CARD[asset]
    assert row["spot"] == pytest.approx(ref["spot"], abs=1)
    for d, v in zip(("7", "30", "90", "180"), ref["atm"], strict=True):
        assert row["atm"][d] is not None and 0.2 < row["atm"][d] < 1.0
        if v is not None:
            assert row["atm"][d] == pytest.approx(v, abs=0.0015)
    # the card rounds to 0.1 vp: BTC 2.65 shown as 2.7; ETH 3.9 vs 4.0 in the prototype
    assert row["put_skew_25"] == pytest.approx(ref["put_skew"], abs=0.0015)
    assert row["put_skew_25"] == pytest.approx(row["smile"]["25P"] - row["smile"]["ATM"], abs=1e-12)
    assert row["rr_25"] == pytest.approx(row["smile"]["25C"] - row["smile"]["25P"], abs=1e-12) and row["rr_25"] < 0
    assert row["smile"]["ATM"] == row["atm"]["30"] and all(v is not None for v in row["smile"].values())
    assert row["day"] == "2026-10-18" and row["ts"] == T_REF // 1000
    days = [e[1] for e in row["expiries"]]
    assert days == sorted(days) and days[0] > 0.5 and days[-1] > 180


# ----------------------------------------------------------------------------- collector job

MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
SPOT = {"BTC": 80_000.0, "ETH": 2_500.0}
NOW = datetime(2026, 10, 21, 21, tzinfo=UTC)  # past 19:00 + the 60 min lag: 21.10 is due


def _name(asset: str, exp: datetime, K: float, cp: str) -> str:
    return f"{asset}-{exp.day}{MON[exp.month - 1]}{exp:%y}-{int(K)}-{cp}"


def _window_trades(asset: str, end_ms: int, *, late: bool = False) -> list[dict]:
    """Flat 50 % smile on expiries 20 and 40 days after the window's day, prints 3 h before 19:00 UTC, 1 s apart.

    ``late``: the same strikes printed 1 s .. 36 s AFTER 19:00 UTC at 80 % IV with the index 10 % higher (look-ahead bait).
    """
    day = end_ms - 19 * HOUR_MS
    spot, out, ts = SPOT[asset], [], end_ms - 3 * HOUR_MS
    vol = 0.5
    if late:
        spot, ts, vol = spot * 1.1, end_ms + 1000, 0.8
    for dd in (20, 40):
        exp = datetime.fromtimestamp(day / 1000, UTC) + timedelta(days=dd, hours=8)
        T = year_frac(int(exp.timestamp() * 1000), ts)
        for m in (0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2):
            K = round(SPOT[asset] * m)
            for cp in ("P", "C"):
                p = inverse_price(spot, K, T, vol, cp == "C")
                name = _name(asset, exp, K, cp)
                out.append({"trade_id": f"{asset}-{ts}", "instrument_name": name, "timestamp": ts, "iv": vol * 100, "price": p})
                out[-1].update({"mark_price": p, "index_price": spot, "direction": "buy"})
                ts += 1000
    return out


def test_cutoff_prints_after_19utc_do_not_change_the_row():
    """No look-ahead: prints after 19:00 UTC (80 % IV, index +10 %) leave the archived row exactly as without them."""
    _, end = window_ms(D0)
    clean = _window_trades("BTC", end)
    late = _window_trades("BTC", end, late=True)
    assert min(t["timestamp"] for t in late) > end
    row = radar_row(build_surface(clean, D0, "BTC"))
    assert row is not None and row["atm"]["30"] == pytest.approx(0.5, abs=1e-4) and row["spot"] == SPOT["BTC"]
    assert radar_row(build_surface(clean + late, D0, "BTC")) == row
    assert radar_row(build_surface(late + clean, D0, "BTC")) == row  # order of the prints does not matter either
    # the bait has teeth: without the cutoff the same prints do change the row
    leaky = build_day(clean + late, D0, ref_hour=float(REF_HOUR_UTC), max_mark_age_h=float(WINDOW_HOURS), product="BTC", cutoff=False)
    assert radar_row(leaky) != row


def _rt(tmp_path, *, fail: set[str] = frozenset(), empty: bool = False, leak: bool = False, **kw) -> tuple[Runtime, list]:
    calls: list = []

    def history(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        asset, start, end = q["currency"], int(q["start_timestamp"]), int(q["end_timestamp"])
        calls.append((asset, start))
        if asset in fail:
            return httpx.Response(500, text="down")
        if empty:
            rows = []
        elif leak:  # a venue that ignores end_timestamp: also returns prints after 19:00 UTC
            rows = [t for t in _window_trades(asset, end) + _window_trades(asset, end, late=True) if t["timestamp"] >= start]
        else:
            rows = [t for t in _window_trades(asset, end) if start <= t["timestamp"] <= end]
        page = rows[:20]  # small pages: the job must paginate
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"trades": page, "has_more": len(rows) > 20}})

    tr = make_transports(State())
    tr["deribit_history"] = httpx.MockTransport(history)
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "radar.sqlite"), **kw), transports=tr, exchange_factory=FakeEx
    )
    rt.now = lambda: NOW
    rt.deribit_history.http._limiter = TokenBucket(1_000.0, 1_000)  # the 5 req/s production limit only slows the test
    rt.deribit_history.http._backoff = 0.0
    return rt, calls


async def test_job_archives_a_week_once(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_radar_archive(rt) == 14  # 7 days x BTC, ETH
        n = len(calls)
        assert n > 14  # paginated: 36 prints per window, 20 per page
        assert await collect_radar_archive(rt) == 0 and len(calls) == n  # stored days are not fetched again
        rows = await rt.store.radar_list("BTC")
        assert [r["day"] for r in rows] == [f"2026-10-{d}" for d in range(15, 22)]
        r = rows[-1]
        assert r["atm_30d"] == pytest.approx(0.5, abs=1e-4) and r["atm_7d"] is None and r["atm_90d"] is None
        assert r["put_skew_25"] == pytest.approx(0.0, abs=1e-4) and r["rr_25"] == pytest.approx(0.0, abs=1e-4)
        assert r["spot"] == SPOT["BTC"] and r["n_prints"] == 36 and r["method"] == METHOD and len(r["expiries"]) == 2
        assert set(r["smile"]) == {"10P", "25P", "35P", "ATM", "35C", "25C", "10C"}
        assert r["ts"] == int(datetime(2026, 10, 21, 19, tzinfo=UTC).timestamp())
        assert await rt.store.radar_insert(dict(rows[-1], atm={}, asset="BTC")) == 0  # PRIMARY KEY (day, asset) holds
    finally:
        await rt.aclose()


async def test_job_ignores_prints_after_19utc(tmp_path):
    """Even when the venue returns prints past the window end, the stored rows equal those of a clean window."""
    (tmp_path / "clean").mkdir()
    (tmp_path / "leaky").mkdir()
    clean, _ = _rt(tmp_path / "clean")
    leaky, _ = _rt(tmp_path / "leaky", leak=True)
    try:
        assert await collect_radar_archive(clean) == 14 and await collect_radar_archive(leaky) == 14
        for asset in ("BTC", "ETH"):
            a, b = await clean.store.radar_list(asset), await leaky.store.radar_list(asset)
            assert [dict(r, created_ts=0) for r in a] == [dict(r, created_ts=0) for r in b]
            assert all(r["atm_30d"] == pytest.approx(0.5, abs=1e-4) and r["spot"] == SPOT[asset] for r in b)
            assert all(r["n_prints"] == 36 for r in b)
    finally:
        await clean.aclose()
        await leaky.aclose()


async def test_job_provider_failure_keeps_other_asset_and_raises(tmp_path):
    rt, _ = _rt(tmp_path, fail={"ETH"})
    try:
        with pytest.raises(RuntimeError, match="fetch failed, will retry"):
            await collect_radar_archive(rt)
        assert len(await rt.store.radar_list("BTC")) == 7 and await rt.store.radar_list("ETH") == []
    finally:
        await rt.aclose()


async def test_job_without_prints_writes_nothing(tmp_path):
    rt, _ = _rt(tmp_path, empty=True)
    try:
        assert await collect_radar_archive(rt) == 0 and await rt.store.radar_list("BTC") == []
    finally:
        await rt.aclose()


async def test_job_needs_a_store():
    rt = Runtime(Settings(db_path=""), transports=make_transports(State()), exchange_factory=FakeEx)
    with pytest.raises(RuntimeError, match="needs a SQLite store"):
        await collect_radar_archive(rt)
    await rt.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    assert Settings().collect_radar_enabled is False
    assert allowed("TRADEDESK_COLLECT_RADAR_ENABLED") and allowed("TRADEDESK_COLLECT_RADAR_SECONDS")
    monkeypatch.setenv("TRADEDESK_COLLECT_RADAR_ENABLED", "1")
    assert Settings.from_env().collect_radar_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_radar_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "radar_archive" not in names(off) and "radar_archive" in names(on)


# ----------------------------------------------------------------------------- snapshot view (radar_view)

AT_1810 = datetime(2026, 10, 18, 21, tzinfo=UTC)  # after 19:00 UTC + the 60 min lag: 18.10 is the newest due day


def _day(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def _inputs(asset: str, now: datetime) -> tuple[list, list, list]:
    d = json.loads(INPUTS.read_text())
    ms = now.timestamp() * 1000
    rows = list(d["rows"][asset].values())
    return rows, [tuple(x) for x in d["dvol"][asset] if x[0] <= ms], [tuple(x) for x in d["closes"][asset] if x[0] <= ms]


def _seed_events(now: datetime) -> list:
    return build_macro_calendar([], _load_seed(), now, 400)["events"]


def test_iv_rank_hand_computed():
    series = [(_day("2025-10-19") + i * DAY_MS, 10.0 + i) for i in range(365)]  # 10 .. 374 %
    last = series[-1][0]
    v, why = iv_rank(series, last)
    assert why is None and v["rank"] == pytest.approx(1.0) and v["percentile"] == pytest.approx(364 / 365)
    assert v["dvol"] == pytest.approx(3.74) and v["min"] == pytest.approx(0.10) and v["n_days"] == 365
    mid, _ = iv_rank([*series[:-1], (last, 192.0)], last)  # (192 - 10) / (373 - 10)
    assert mid["rank"] == pytest.approx(182 / 363) and mid["percentile"] == pytest.approx(182 / 365)
    assert iv_rank(series, last + DAY_MS) == (None, "no DVOL close for 2026-10-19")
    assert iv_rank(series[-100:], last)[1] == "DVOL history: 100 of 365 days"
    assert iv_rank([(t, 40.0) for t, _ in series], last)[1] == "DVOL flat over the window"


def test_iv_rank_matches_the_lib_table():
    """LIB-20261018 «Звірка радару»: our IV Rank on daily DVOL over 365 days, rounded to 0.1 %."""
    _, dvol, _ = _inputs("BTC", datetime(2026, 10, 22, tzinfo=UTC))
    lib = {"2026-09-02": 0.069, "2026-09-13": 0.104, "2026-09-27": 0.030, "2026-10-04": 0.059, "2026-10-11": 0.076}
    for day, ref in lib.items():
        v, why = iv_rank(dvol, _day(day))
        assert why is None and v["rank"] == pytest.approx(ref, abs=0.0015), day
    # the card's 10 % for 18.10 was the live close at 15:13 UTC (38.49); the final daily close is 38.82
    v, _ = iv_rank(dvol, _day("2026-10-18"))
    assert v["rank"] == pytest.approx(0.10, abs=0.01) and v["min"] == pytest.approx(0.3359) and v["max"] == pytest.approx(0.8262)


def test_trailing_rv_hand_computed_and_gaps():
    now = datetime(2026, 10, 18, 15, tzinfo=UTC)
    d0 = _day("2026-10-18")
    closes = [(d0 - (30 - i) * DAY_MS, 100.0 * (1.02 if i % 2 else 1.0)) for i in range(31)]
    rv, why = trailing_rv(closes, now, 30)
    assert why is None and rv == pytest.approx(math.sqrt(365) * math.log(1.02), rel=1e-12)
    assert trailing_rv(closes, now, 7)[0] == pytest.approx(math.sqrt(365) * math.log(1.02), rel=1e-12)
    assert trailing_rv(closes[:-1], now, 30) == (None, "30 daily closes, need 31")
    assert trailing_rv([*closes[:5], *closes[6:]], now, 7)[0] is not None  # the gap is outside the 7-day window
    assert trailing_rv([*closes[:-5], *closes[-4:]], now, 7) == (None, "gap in the daily closes")
    stale = trailing_rv(closes, now + timedelta(days=3), 30)
    assert stale == (None, "newest daily close 2026-10-18 is stale")
    assert trailing_rv([(t + 16 * HOUR_MS, c) for t, c in closes], now, 30)[0] is None  # a bar after `now` is not used


def test_forward_vols_hand_computed_and_events():
    t0, t1, t2 = 1_793_347_200, 1_793_952_000, 1_795_766_400  # 30.10, 06.11, 27.11 08:00 UTC
    events = [
        {"ts_utc": "2026-10-30T08:00:00+00:00", "kind": "fomc", "title": "on t0: excluded"},
        {"ts_utc": "2026-11-03T12:00:00+00:00", "kind": "election", "title": "Mid\x00terms\n <b>x</b>"},
        {"ts_utc": "2026-11-06T08:00:00+00:00", "kind": "CPI", "title": "on t1: included"},
        {"ts_utc": "2026-11-05T13:30:00+00:00", "kind": "jobless_claims", "title": "not a forward-vol kind"},
        {"ts_utc": "2026-11-04T00:00:00", "kind": "nfp", "title": "no timezone: dropped"},
        {"ts_utc": "bad", "kind": "nfp"},
        "not a dict",
    ]
    exp = [[t1, 20.0, 1.0, 0.5], [t0, 10.0, 1.0, 0.4], [t2, 40.0, 1.0, 0.3], [t2 + 1, 50.0, 1.0, None]]
    fv = forward_vols(exp, events)
    w1, w2 = fv["windows"]
    assert w1["fwd"] == pytest.approx(math.sqrt((0.25 * 20 - 0.16 * 10) / 10)) and w1["days"] == 10.0
    assert w1["from_utc"] == "2026-10-30T08:00:00Z" and w1["to_utc"] == "2026-11-06T08:00:00Z"
    assert [e["kind"] for e in w1["events"]] == ["election", "cpi"]
    assert w1["events"][0]["title"] == "Mid terms <b>x</b>"  # control characters dropped; HTML is escaped at render time
    assert w2["fwd"] is None and "non-positive" in w2["reason"]  # 0.09 * 40 < 0.25 * 20
    assert fv["median_weekly"] == pytest.approx(w1["fwd"]) and w1["hot"] is False  # the only weekly window is the median


def test_normalize_row_store_shape_equals_row_shape():
    row = json.loads(INPUTS.read_text())["rows"]["BTC"]["2026-10-18"]
    stored = {k: v for k, v in row.items() if k != "atm"}
    stored.update({f"atm_{d}d": row["atm"][d] for d in ("7", "30", "90", "180")}, asset="BTC", created_ts=0)
    assert normalize_row(stored) == normalize_row(row)


def test_radar_view_real_sample_matches_the_lib_and_card():
    """BTC 18.10 (LIB-20261018 and the card's «Підсумки 18.10»): price, ATM 7/30/90/180, skew, IV Rank, forward vol."""
    rows, dvol, closes = _inputs("BTC", AT_1810)
    v = radar_view("BTC", rows, dvol, closes, _seed_events(AT_1810), AT_1810)
    assert v["missing"] == []
    assert v["surface"]["now_day"] == "2026-10-18" and v["surface"]["week_day"] == "2026-10-11"
    assert v["surface"]["ref_utc"] == "2026-10-18T19:00:00Z" and v["surface"]["method"] == METHOD
    p = v["price"]
    assert p["now"] == pytest.approx(81_093, abs=1) and p["week"] == pytest.approx(83_565, abs=1)
    assert p["change"] == pytest.approx(-0.030, abs=0.0005)
    for d, now_ref, week_ref in zip(("7", "30", "90", "180"), (0.341, 0.370, 0.371, 0.382), (0.322, 0.355, 0.374, 0.384), strict=True):
        assert v["atm"]["now"][d] == pytest.approx(now_ref, abs=0.0015), d
        assert v["atm"]["week"][d] == pytest.approx(week_ref, abs=0.0015), d
        assert v["atm"]["change"][d] == pytest.approx(v["atm"]["now"][d] - v["atm"]["week"][d], abs=1e-12)
    sk = v["skew"]
    assert sk["now"]["put_skew_25"] == pytest.approx(0.0265, abs=0.0005)  # LIB: 2.65 vp
    assert sk["week"]["put_skew_25"] == pytest.approx(0.022, abs=0.0005)  # card: 2.2 -> 2.7
    assert sk["now"]["rr_25"] < 0 and sk["change"]["put_skew_25"] == pytest.approx(0.0046, abs=0.0005)
    assert v["smile"]["now"]["ATM"] == pytest.approx(v["atm"]["now"]["30"]) and set(v["smile"]["week"]) == set(v["smile"]["now"])
    r = v["iv_rank"]
    assert r["now"]["day"] == "2026-10-18" and r["now"]["rank"] == pytest.approx(0.10, abs=0.01)
    assert r["week"]["day"] == "2026-10-11" and r["week"]["rank"] == pytest.approx(0.076, abs=0.0015)
    assert 0 < r["now"]["percentile"] < 0.5 and r["change"] == pytest.approx(r["now"]["rank"] - r["week"]["rank"])
    rv = v["iv_vs_rv"]
    cl = [c for _, c in closes]
    assert rv["rv30"] == pytest.approx(realized_vol(cl[-31:]), rel=1e-12) and rv["rv7"] == pytest.approx(realized_vol(cl[-8:]), rel=1e-12)
    assert rv["rv30"] == pytest.approx(0.316, abs=0.005)  # the card: 31.6 % (its last close was live at 15:13)
    assert rv["premium_rv30"] == pytest.approx(rv["iv30"] - rv["rv30"]) and rv["iv30"] == v["atm"]["now"]["30"]
    fv = v["forward_vol"]
    by_from = {w["from_utc"][:10]: w for w in fv["windows"]}
    elec = by_from["2026-10-30"]  # LIB H4: 30.10 -> 06.11 at 42 % against 34-37 % around it; US midterms 03.11
    assert elec["to_utc"] == "2026-11-06T08:00:00Z" and elec["fwd"] == pytest.approx(0.42, abs=0.005) and elec["hot"]
    assert [e["kind"] for e in elec["events"]] == ["election"]
    fomc = by_from["2026-10-23"]  # FOMC 28.10 is in 23.10 -> 30.10 and carries no premium there (34 %)
    assert fomc["fwd"] == pytest.approx(0.34, abs=0.005) and not fomc["hot"] and [e["kind"] for e in fomc["events"]] == ["fomc"]
    assert [w["from_utc"][:10] for w in fv["windows"] if w["hot"]] == ["2026-10-30"]
    assert 0.34 < fv["median_weekly"] < 0.37


def test_radar_view_ignores_dvol_and_closes_after_now():
    """The fixture runs to 22.10: fed unfiltered at 18.10 21:00 UTC, the view equals the one on data up to `now`."""
    d = json.loads(INPUTS.read_text())
    rows, dvol, closes = _inputs("BTC", AT_1810)
    later = [x for x in d["dvol"]["BTC"] if x[0] > AT_1810.timestamp() * 1000]
    assert later and len(closes) < len(d["closes"]["BTC"])  # the unfiltered series really has future points
    events = _seed_events(AT_1810)
    full = radar_view("BTC", rows, [tuple(x) for x in d["dvol"]["BTC"]], [tuple(x) for x in d["closes"]["BTC"]], events, AT_1810)
    cut = radar_view("BTC", rows, dvol, closes, events, AT_1810)
    assert full["iv_rank"] == cut["iv_rank"] and full["iv_vs_rv"] == cut["iv_vs_rv"]
    assert full["iv_rank"]["now"]["day"] == "2026-10-18" and full == cut


def test_radar_view_two_day_old_row_drops_ended_windows():
    """21.10 19:30 UTC: the newest due day is 20.10, so the 18.10 row is «now»; its 20.10 -> 21.10 window has ended."""
    at = datetime(2026, 10, 21, 19, 30, tzinfo=UTC)
    rows, dvol, closes = _inputs("BTC", at)
    v = radar_view("BTC", rows, dvol, closes, _seed_events(at), at)
    assert v["surface"]["now_day"] == "2026-10-18"
    fresh = radar_view("BTC", rows, dvol, closes, _seed_events(AT_1810), AT_1810)["forward_vol"]["windows"]
    assert fresh[0]["to_utc"] == "2026-10-21T08:00:00Z"  # present while it had not ended
    windows = v["forward_vol"]["windows"]
    assert windows and all(w["to_utc"] > "2026-10-21T19:30:00Z" for w in windows)
    assert len(windows) == len(fresh) - 1 and windows[0]["from_utc"] == "2026-10-21T08:00:00Z"


def test_radar_view_eth_real_sample():
    rows, dvol, closes = _inputs("ETH", AT_1810)
    v = radar_view("ETH", rows, dvol, closes, _seed_events(AT_1810), AT_1810)
    assert v["missing"] == [] and v["price"]["now"] == pytest.approx(2_363, abs=1)
    assert v["price"]["change"] == pytest.approx(-0.066, abs=0.0005)  # card: -6.6 %
    assert v["atm"]["now"]["7"] == pytest.approx(0.520, abs=0.0015) and v["atm"]["week"]["7"] == pytest.approx(0.422, abs=0.0015)
    assert v["atm"]["now"]["7"] > v["atm"]["now"]["30"]  # the card's front inversion
    assert v["skew"]["now"]["put_skew_25"] == pytest.approx(0.039, abs=0.0015)  # prototype 4.0


def test_radar_view_without_archive_is_null_with_reasons():
    """No archive rows (the job is off): surface fields are None with a reason; IV Rank and RV still come; no exception."""
    _, dvol, closes = _inputs("BTC", AT_1810)
    v = radar_view("BTC", [], dvol, closes, [], AT_1810)
    assert v["price"] == {"now": None, "week": None, "change": None}
    assert v["atm"] == {"now": None, "week": None, "change": None} and v["skew"]["now"] is None and v["smile"]["now"] is None
    assert v["forward_vol"] == {"windows": [], "median_weekly": None}
    assert v["iv_vs_rv"]["iv30"] is None and v["iv_vs_rv"]["premium_rv30"] is None and v["iv_vs_rv"]["rv30"] is not None
    assert v["iv_rank"]["now"] is not None
    assert v["missing"] == [
        {"field": "surface", "reason": "no radar archive row from 2026-10-16 to 2026-10-18 (job radar_archive off or behind)"}
    ]
    # nothing at all: every block null, all reasons listed
    empty = radar_view("BTC", [], [], [], [], AT_1810)
    assert {m["field"] for m in empty["missing"]} == {"surface", "iv_rank", "rv30", "rv7"} and empty["iv_rank"]["now"] is None


def test_radar_view_week_row_missing_and_stale_now_row():
    rows, dvol, closes = _inputs("BTC", AT_1810)
    only_now = [r for r in rows if r["day"] == "2026-10-18"]
    v = radar_view("BTC", only_now, dvol, closes, [], AT_1810)
    assert v["price"]["now"] is not None and v["price"]["week"] is None and v["price"]["change"] is None
    assert v["atm"]["week"] is None and v["atm"]["change"] is None and v["skew"]["change"] is None
    assert v["missing"] == [{"field": "surface_week", "reason": "no radar archive row for 2026-10-11"}]
    # before 20:00 UTC on 18.10 its row is not due yet (newest due day 17.10); three days later it is too old for «now»
    early = datetime(2026, 10, 18, 19, 30, tzinfo=UTC)
    assert radar_view("BTC", only_now, dvol, closes, [], early)["surface"]["now_day"] is None
    later = AT_1810 + timedelta(days=3)
    assert radar_view("BTC", only_now, dvol, closes, [], later)["surface"]["now_day"] is None
    assert radar_view("BTC", [*only_now, {"day": "bad"}, {"day": "2026-10-17"}], dvol, closes, [], AT_1810)["price"]["now"] is not None
