"""Smart-money forward slice analytics and runner mechanics (IDEA-20261015-smart-money-persistence, slice 1).

Outcomes here are synthetic: the runner's full stage is exercised on a stub store and a stub cache only."""

from __future__ import annotations

import importlib.util
import json
import math
import sqlite3
import statistics
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tradedesk.analytics import smart_money_forward as smf
from tradedesk.analytics.smart_money import replay_positions

ROOT = Path(__file__).resolve().parent.parent


def test_complete_periods_and_matured_days():
    as_of = date(2026, 11, 25)
    assert smf.complete_periods(date(2026, 10, 16), 30, as_of) == [(date(2026, 10, 16), date(2026, 11, 15))]
    assert smf.complete_periods(date(2026, 10, 16), 30, date(2026, 11, 15)) == []
    assert smf.complete_periods(date(2026, 10, 16), 30, date(2026, 11, 16)) == [(date(2026, 10, 16), date(2026, 11, 15))]
    weeks = smf.complete_periods(date(2026, 10, 16), 7, as_of)
    assert [e for _, e in weeks] == [date(2026, 10, 23), date(2026, 10, 30), date(2026, 11, 6), date(2026, 11, 13), date(2026, 11, 20)]
    d7 = smf.matured_days(date(2026, 10, 17), date(2026, 11, 24), 7, as_of)
    assert d7[0] == date(2026, 10, 17) and d7[-1] == date(2026, 11, 17)
    assert smf.matured_days(date(2026, 10, 17), date(2026, 11, 24), 1, as_of)[-1] == date(2026, 11, 23)


def _snap(n: int, **over: dict) -> dict:
    s = {
        f"a{i:02d}": {"account_value": 1e6, "vlm_month": 1e6, "pnl_month": float(i), "pnl_all": float(10 * i), "pnl_week": float(i)}
        for i in range(n)
    }
    for a, r in over.items():
        s[a] = {**s.get(a, {}), **r}
    return s


def test_h1_universe_excludes_small_and_classes():
    snap = _snap(5, a00={"account_value": 400_000.0}, a01={"pnl_month": None})
    cls = {"a02": "mm", "a03": "system", "a04": "hft_directional"}
    assert smf.h1_universe(snap, cls, "pnl_month") == ["a04"]
    assert smf.h1_class({"account_value": 1e6, "vlm_month": 2e8}, None) == "mm"
    assert smf.h1_class({"account_value": 1e6, "vlm_month": 1e6}, []) == "idle"


def test_h1_outcomes_fallback_and_missing():
    uni = ["a", "b", "c"]
    start = {a: {"account_value": 1e6} for a in uni}
    end = {"a": {"pnl_month": 1e5}, "b": {"pnl_month": None}}
    roi, src = smf.h1_outcomes(uni, start, end, "pnl_month", {"b": -2e5, "c": None})
    assert roi == {"a": pytest.approx(0.1), "b": pytest.approx(-0.2)}
    assert src == {"a": "snapshot", "b": "portfolio", "c": "missing"}


def test_h1_period_missing_and_top_drop():
    uni = [f"a{i:02d}" for i in range(20)]
    sig = {a: float(i) for i, a in enumerate(uni)}
    roi = {a: float(i) / 100 for i, a in enumerate(uni) if i != 19}  # the best signal has no outcome
    m = smf.h1_period(uni, sig, roi)
    assert m["n"] == 19 and m["ic"] == pytest.approx(1.0)
    m1 = smf.h1_period(uni, sig, roi, missing_as=-1.0)
    assert m1["n"] == 20 and m1["ic"] < 1.0
    mt = smf.h1_period(uni, {**sig, "a00": -100.0}, roi, drop_top_frac=0.01)  # top 1 % by |signal| = a00
    assert mt["n"] == 18


def test_portfolio_change_finest_window():
    pf = {
        "week": {"pnl": [(500, 0.0), (1000, 10.0)]},
        "month": {"pnl": [(0, 0.0), (1000, 50.0)]},
        "allTime": {"pnl": [(-10, 0.0), (2000, 100.0)]},
    }
    assert smf.portfolio_change(pf, 600, 1000) == pytest.approx(8.0)  # week covers
    assert smf.portfolio_change(pf, 100, 1000) == pytest.approx(45.0)  # month
    assert smf.portfolio_change(pf, -5, 1500) is not None  # allTime
    assert smf.portfolio_change(pf, -100, 1500) is None


def test_tracked_from_snapshot():
    snap = _snap(5, a00={"pnl_all": 999.0, "account_value": 1e5})
    assert smf.tracked_from_snapshot(snap, 1) == {"a04", "a00"}
    assert smf.tracked_from_snapshot(snap, 1, 500_000.0) == {"a04"}


ROWS = [
    {"ts": 10, "account": "x", "market": "BTC", "side": "long", "size": 1.0, "value_usd": 100.0},
    {"ts": 10, "account": "y", "market": "ETH", "side": "short", "size": 2.0, "value_usd": 20.0},
    {"ts": 20, "account": "x", "market": "SOL", "side": "long", "size": 5.0, "value_usd": 50.0},
    {"ts": 30, "account": "x", "market": "BTC", "side": "closed", "size": 0.0, "value_usd": 0.0},
    {"ts": 30, "account": "y", "market": "ETH", "side": "short", "size": 3.0, "value_usd": 30.0},
    {"ts": 40, "account": "y", "market": "DOGE", "side": "long", "size": 9.0, "value_usd": 9.0},
]


def test_replay_checkpoints_matches_replay_positions():
    cps = [5, 10, 25, 30, 100]
    got = dict(smf.replay_checkpoints(ROWS, 10, cps))
    for c in cps:
        assert got[c] == replay_positions(ROWS, 10, until=c)


def test_replay_checkpoints_no_lookahead():
    later = [*ROWS, {"ts": 50, "account": "x", "market": "SOL", "side": "closed", "size": 0.0, "value_usd": 0.0}]
    a = dict(smf.replay_checkpoints(ROWS, 10, [30]))
    b = dict(smf.replay_checkpoints(later, 10, [30]))
    assert a == b


def test_first_run_at_or_after():
    runs = [100, 3700, 90_000]
    assert smf.first_run_at_or_after(runs, 0, 200) == 100
    assert smf.first_run_at_or_after(runs, 200, 3600) == 3700
    assert smf.first_run_at_or_after(runs, 4000, 7200) is None


def test_strong_accounts():
    snap = _snap(10)
    assert smf.strong_accounts(snap, {}, ["a09", "a08", "a01"]) == {"a09", "a08"}  # top 20 % of 10 = 2


def test_relative_return():
    d = date(2026, 10, 20)
    c = {d: 100.0, date(2026, 10, 27): 110.0}
    b = {d: 50.0, date(2026, 10, 27): 52.5}
    assert smf.relative_return(c, b, d, 7) == pytest.approx(0.10 - 0.05)
    assert smf.relative_return(c, b, d, 1) is None


def test_h2_day_ic_and_spread():
    sig = {f"c{i:02d}": float(i - 5) for i in range(11)}  # c05 has a zero signal and is not a coin of the day
    out = {c: s / 100 for c, s in sig.items()}
    m = smf.h2_day(sig, out)
    assert m["n"] == 10 and m["ic"] == pytest.approx(1.0)
    assert m["spread"] == pytest.approx((0.05 + 0.04) / 2 - (-0.05 - 0.04) / 2)
    assert m["spread_net"] == pytest.approx(m["spread"] - 0.006)
    assert smf.h2_day(dict(list(sig.items())[:9]), out)["ic"] is None


def test_nw_summary_lag0_is_iid_se():
    xs = [0.1, -0.2, 0.3, 0.05, 0.0]
    s = smf.nw_summary(xs, 0)
    assert s["se"] == pytest.approx(statistics.pstdev(xs) / math.sqrt(len(xs)))
    assert s["mde"] == pytest.approx(2.8 * s["se"])


PM = [
    {"ts": 100, "account": "w", "market": "t1", "side": "Yes", "size": 100.0, "value_usd": 40.0, "entry_px": 0.35, "title": "A"},
    {"ts": 200, "account": "w", "market": "t2", "side": "No", "size": 10.0, "value_usd": 6.0, "entry_px": 0.6, "title": "B"},
    {"ts": 300, "account": "w", "market": "t3", "side": "Yes", "size": 10.0, "value_usd": 1.0, "entry_px": 0.1, "title": "C"},
    {"ts": 300, "account": "v", "market": "t4", "side": "Yes", "size": 10.0, "value_usd": 1.0, "entry_px": 0.1, "title": "D"},
    {"ts": 400, "account": "v", "market": "t5", "side": "Yes", "size": 10.0, "value_usd": 5.0, "entry_px": 0.5, "title": "E"},
]


def test_new_positions_baseline_rebaseline_and_cutoff():
    ev = smf.new_positions(PM, 50, 1000, rebaseline=[300])
    assert [(e["account"], e["market"]) for e in ev] == [("w", "t2"), ("v", "t5")]  # t1 / t4 baselines, t3 rebaseline
    assert ev[0]["p_obs"] == pytest.approx(0.6) and ev[0]["n_before"] == 1
    later = [*PM, {"ts": 500, "account": "w", "market": "t9", "side": "Yes", "size": 1.0, "value_usd": 0.5, "title": "F"}]
    assert smf.new_positions(later, 50, 400, rebaseline=[300]) == ev  # rows after the cut-off do not change it


def test_gamma_resolution():
    as_of = int(datetime(2026, 11, 25, tzinfo=UTC).timestamp())
    m = {
        "clobTokenIds": json.dumps(["t1", "t2"]),
        "outcomePrices": json.dumps(["1", "0"]),
        "closed": True,
        "closedTime": "2026-11-20 10:00:00+00",
    }
    assert smf.gamma_resolution(m, "t1", as_of) == (1.0, "resolved")
    assert smf.gamma_resolution(m, "t2", as_of) == (0.0, "resolved")
    assert smf.gamma_resolution(m, "t9", as_of)[1] == "token_not_in_market"
    assert smf.gamma_resolution({**m, "closed": False}, "t1", as_of)[1] == "open"
    assert smf.gamma_resolution({**m, "closedTime": "2026-11-26T00:00:00Z"}, "t1", as_of)[1] == "closed_after_as_of"
    assert smf.gamma_resolution({**m, "outcomePrices": '["0.5", "0.5"]'}, "t1", as_of)[1] == "not_binary_resolved"
    assert smf.gamma_resolution({**m, "closedTime": None, "endDate": None}, "t1", as_of)[1] == "no_close_time"


def test_cluster_mean_ci_analytic():
    # two clusters of two identical values: cluster sums of deviations +-1, CR1 var = 2 * 2 / 16
    c = smf.cluster_mean_ci([0.5, 0.5, -0.5, -0.5], ["a", "a", "b", "b"])
    assert c["mean"] == pytest.approx(0.0) and c["clusters"] == 2
    assert c["se"] == pytest.approx(math.sqrt(2 * (1 + 1) / 16))
    assert c["hi"] == pytest.approx(12.706 * c["se"])
    assert smf.cluster_mean_ci([1.0], ["a"])["se"] is None
    assert smf.h3_edge(1.0, 0.6) == pytest.approx(0.39)


def test_cochran_q_analytic():
    same = smf.cochran_q([0.1, 0.1, 0.1], [0.05, 0.05, 0.05])
    assert same["q"] == pytest.approx(0.0) and same["i2"] == 0.0
    two = smf.cochran_q([0.0, 1.0], [1.0, 1.0])  # pooled 0.5, Q = 0.25 + 0.25
    assert two["pooled"] == pytest.approx(0.5) and two["q"] == pytest.approx(0.5) and two["i2"] == 0.0
    far = smf.cochran_q([0.0, 10.0], [1.0, 1.0])  # Q = 50, I2 = 49 / 50
    assert far["i2"] == pytest.approx(0.98)
    assert smf.cochran_q([0.1], [0.1])["q"] is None


# ---------------------------------------------------------------------------------------------------------------------
# Runner mechanics on a stub store and stub cache (synthetic outcomes)


def _load_runner():
    spec = importlib.util.spec_from_file_location("verify_smart_money_slice1", ROOT / "scripts" / "verify_smart_money_slice1.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _ts(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=UTC).timestamp())


def _schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE trader_snapshot (day TEXT, venue TEXT, account TEXT, ts INTEGER, name TEXT, account_value REAL, pnl_day REAL, "
        "pnl_week REAL, pnl_month REAL, pnl_all REAL, roi_month REAL, roi_all REAL, vlm_month REAL, rank_month INTEGER)"
    )
    con.execute(
        "CREATE TABLE trader_positions (ts INTEGER, venue TEXT, account TEXT, market TEXT, side TEXT, size REAL, entry_px REAL, "
        "value_usd REAL, upnl REAL, leverage REAL, liq_px REAL, title TEXT)"
    )
    con.execute("CREATE TABLE trader_polls (venue TEXT, ts INTEGER, account TEXT, ok INTEGER)")


def _stub_store(path: Path) -> None:
    con = sqlite3.connect(path)
    _schema(con)
    accounts = [f"0x{i:02x}" for i in range(30)]
    for k in range(41):  # 2026-10-16 .. 2026-11-25
        d = date.fromordinal(date(2026, 10, 16).toordinal() + k)
        ts = _ts(d.year, d.month, d.day, 0, 5)
        for i, a in enumerate(accounts):
            con.execute(
                "INSERT INTO trader_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d.isoformat(),
                    "hyperliquid",
                    a,
                    ts,
                    None,
                    1e6,
                    0,
                    float(i * (k % 3 + 1)),
                    float(i * (k + 1)),
                    float(i * k * 2),
                    None,
                    None,
                    1e6,
                    None,
                ),
            )
        for i, w in enumerate(["0xw1", "0xw2"]):
            con.execute(
                "INSERT INTO trader_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.isoformat(), "polymarket", w, ts, None, None, 0, 0, float(i), float(i), None, None, 0, None),
            )
        run = _ts(d.year, d.month, d.day, 0, 15)
        for i, a in enumerate(accounts):  # every account trades every coin each day: size grows with the account index
            for j, coin in enumerate(["BTC", *[f"C{x}" for x in range(11)]]):
                size = 1.0 + (i + 1) * (j + 1) * (k + 1) * (1 if (i + j) % 2 else -1) / 100
                side = "long" if size > 0 else "short"
                con.execute(
                    "INSERT INTO trader_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run, "hyperliquid", a, coin, side, abs(size), 1.0, abs(size) * 10, 0, 1, None, None),
                )
    pm = [
        (_ts(2026, 10, 20), "0xw1", "tokA", "Yes", 100.0, 40.0, 0.4, "Market A"),
        (_ts(2026, 10, 21), "0xw1", "tokB", "Yes", 100.0, 30.0, 0.3, "Market B"),
        (_ts(2026, 10, 21), "0xw2", "tokC", "No", 100.0, 50.0, 0.5, "Market C"),
        (_ts(2026, 10, 22), "0xw2", "tokD", "Yes", 100.0, 20.0, 0.2, "Market D"),
        (_ts(2026, 10, 22), "0xw1", "closedX", "closed", 0.0, 0.0, None, None),
    ]
    for ts, w, mk, side, size, val, ep, title in pm:
        con.execute(
            "INSERT INTO trader_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "polymarket", w, mk, side, size, ep, val, 0, None, None, title),
        )
    con.commit()
    con.close()


def _stub_cache(cache: Path) -> None:
    coins = ["BTC", *[f"C{x}" for x in range(11)]]
    for j, c in enumerate(coins):
        rows = []
        for k in range(41):
            t = _ts(2026, 10, 15) * 1000 + k * 86_400_000
            rows.append({"t": t, "c": 100.0 * (1 + 0.001 * j) ** k})
        (cache / "candles").mkdir(parents=True, exist_ok=True)
        (cache / "candles" / f"{c}.json").write_text(json.dumps(rows))
    (cache / "gamma").mkdir(parents=True, exist_ok=True)
    mk = []
    for tok, win in (("tokB", "1"), ("tokC", "0"), ("tokD", "1")):
        mk.append({"conditionId": f"cond_{tok}", "clobTokenIds": json.dumps([tok, f"other_{tok}"]), "outcomePrices": json.dumps([win, "1" if win == "0" else "0"]),
                   "closed": True, "closedTime": "2026-11-01T00:00:00Z", "endDate": "2026-11-01T00:00:00Z"})  # fmt: skip
    (cache / "gamma" / "batch_00000.json").write_text(json.dumps({"tokens": ["tokB", "tokD"], "markets": mk}))


class _FakeGamma:
    def __init__(self, open_markets: dict, closed_markets: dict) -> None:
        self.open, self.closed, self.calls = open_markets, closed_markets, []

    async def get_json(self, path, params, ttl, tool, args):
        from tradedesk.core.envelope import Fetched  # noqa: PLC0415

        self.calls.append(dict(params))
        pool = self.closed if params.get("closed") == "true" else self.open
        data = [m for t, m in pool.items() if t in params["clob_token_ids"]]
        return Fetched(data, None)


def test_fetch_gamma_batches_and_closed_retry(tmp_path):
    import asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    runner = _load_runner()
    runner.GAMMA_BATCH = 2
    m = lambda t: {"clobTokenIds": json.dumps([t]), "closed": False, "secret": "dropped"}  # noqa: E731
    g = _FakeGamma({"a": m("a"), "c": m("c")}, {"b": {**m("b"), "closed": True}})
    rt = SimpleNamespace(polymarket=SimpleNamespace(gamma=g))
    from collections import Counter  # noqa: PLC0415

    asyncio.run(runner.fetch_gamma(rt, tmp_path, ["a", "b", "c"], Counter()))
    gm = runner.gamma_map(tmp_path)
    assert sorted(gm) == ["a", "b", "c"] and gm["b"]["closed"] is True and "secret" not in gm["a"]
    assert [c.get("closed") for c in g.calls] == [None, "true", None]  # batch [a, b] -> retry [b] closed; batch [c]
    n = len(g.calls)
    asyncio.run(runner.fetch_gamma(rt, tmp_path, ["a", "b", "c"], Counter()))
    assert len(g.calls) == n  # cached: no refetch


def test_runner_coverage_and_full_on_stub(tmp_path):
    runner = _load_runner()
    db = tmp_path / "store.sqlite"
    _stub_store(db)
    cache = tmp_path / "cache"
    _stub_cache(cache)
    conn = runner.cmn.connect_ro(db)
    as_of = datetime(2026, 11, 25, tzinfo=UTC)
    prep = runner.Prep(conn, as_of, tmp_path / "no_stage0")
    cov = runner.coverage(prep)
    assert cov["h1"][0]["universe"] == 30 and cov["h1"][0]["missing"] == 0
    assert cov["h2_h7"]["matured"] == 32
    assert cov["h3"]["new"] == 2  # tokA / tokC are the wallets' baselines; tokB and tokD are new
    res = runner.full(prep, cache, seed=1, no_fetch=True)
    h1 = res["h1"]["periods"][0]
    assert h1["n"] == 30 and h1["ic"] == pytest.approx(1.0)  # synthetic: end PnL proportional to the signal
    assert res["h1"]["ic"]["gate"] is False  # T = 1: MDE = 2.8 * retro sd
    assert len(res["h1w"]["periods"]) == 5
    assert res["h2"]["h7"]["used"] > 0
    assert res["h3"]["samples"] == {"first:main": 2} and res["h3"]["data_quality"] == {"ok": 2}
    assert res["h3"]["overall"]["main"]["n"] == 2  # synthetic: both won, p_obs 0.3 and 0.2
    assert res["h3"]["overall"]["main"]["mean"] == pytest.approx(((1 - 0.3) + (1 - 0.2)) / 2 - 0.01)
    assert res["h3"]["overall"]["registered"] is False  # 2 markets < 30
    assert res["h3"]["overall"]["status"] == "not_candidate" and res["h3"]["overall"]["guards"]["fresh_entry"]["n"] == 2
    assert res["h2"]["decision"]["verdict"] in ("inconclusive", "confirmed", "rejected") and "h7_entry_0100" in res["h2"]
    again = runner.full(prep, cache, seed=1, no_fetch=True)
    assert json.dumps(again, default=str, sort_keys=True) == json.dumps(res, default=str, sort_keys=True)  # deterministic


# ---------------------------------------------------------------------------------------------------------------------
# Slice-1 fix round (critic review 2026-11-26, before any outcome): B1-B4, M1, M3, m2, m3. Each fails on the code
# reviewed by the critic (missing behaviour, key or argument).


def test_t_quantile_and_decision_level():
    assert smf.t_quantile(0.975, 10) == pytest.approx(2.2281, abs=1e-3)
    assert smf.t_quantile(0.995, 30) == pytest.approx(2.7500, abs=1e-3)
    assert smf.t_quantile(0.975, 1) == pytest.approx(12.706, abs=1e-2)
    assert smf.DECISION_LEVEL == pytest.approx(1 - 0.05 / 6)
    assert smf.z_two_sided(0.95) == pytest.approx(1.95996, abs=1e-4)
    vals, cl = [0.5, 0.5, -0.5, -0.5, 0.2, 0.1], list("aabbcc")
    c95, c99 = smf.cluster_mean_ci(vals, cl), smf.cluster_mean_ci(vals, cl, level=0.99)
    assert c99["hi"] - c99["mean"] == pytest.approx(c95["se"] * 9.925, rel=1e-3)  # t(0.995, 2)


def _pm_snap(day_rows: dict) -> dict:
    return {d: {a: {"ts": ts, "pnl_month": p, "pnl_all": p, "account_value": None} for a, ts, p in rows} for d, rows in day_rows.items()}


def test_watch_list_at_uses_only_rows_written_before_t():  # B1
    d0, d1 = date(2026, 10, 19), date(2026, 10, 20)
    snaps = _pm_snap(
        {
            d0: [("a", _ts(2026, 10, 19, 18), 5.0), ("b", _ts(2026, 10, 19, 18), 4.0)],
            d1: [("a", _ts(2026, 10, 20, 6), 1.0), ("x", _ts(2026, 10, 20, 18), 99.0)],
        }
    )
    w = smf.WatchListAt(snaps, top_n=1)
    assert w.at(_ts(2026, 10, 20, 0, 30)) == {"a"}  # the day's snapshot is not written yet: 10-19's list
    assert w.at(_ts(2026, 10, 20, 7)) == {"a"}  # 10-20 still being written (row at 18:00): 10-19's list, not a partial one
    assert w.at(_ts(2026, 10, 20, 19)) == {"x"}
    assert w.at(_ts(2026, 10, 19, 1)) == frozenset()
    assert w.at(_ts(2026, 10, 19, 18)) == frozenset()  # a row written at t is not visible at t
    later = {**snaps, date(2026, 10, 21): {"y": {"ts": _ts(2026, 10, 21, 1), "pnl_month": 1e9, "pnl_all": 1e9, "account_value": None}}}
    assert smf.WatchListAt(later, 1).at(_ts(2026, 10, 20, 7)) == {"a"}  # rows after t change nothing before t


def _pm_only_store(path: Path, snaps: list, positions: list) -> None:
    con = sqlite3.connect(path)
    _schema(con)
    for day, venue, a, ts, pnl in snaps:
        con.execute(
            "INSERT INTO trader_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, venue, a, ts, None, None, 0, 0, pnl, pnl, None, None, 0, None),
        )
    for ts, a, mk, side, size, val, ep in positions:
        con.execute(
            "INSERT INTO trader_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "polymarket", a, mk, side, size, ep, val, 0, None, None, mk),
        )
    con.commit()
    con.close()


def test_runner_h3_ranking_at_event_time(tmp_path):  # B1: a wallet that enters the list only in the 18:00 snapshot
    runner = _load_runner()
    db = tmp_path / "pm.sqlite"
    snaps = [("2026-10-19", "polymarket", "w1", _ts(2026, 10, 19, 18), 1.0), ("2026-10-20", "polymarket", "w1", _ts(2026, 10, 20, 18), 1.0)]
    snaps.append(("2026-10-20", "polymarket", "w9", _ts(2026, 10, 20, 18), 1e6))  # w9: only in the day's 18:00 snapshot
    pos = []
    for w in ("w1", "w9"):
        pos += [
            (_ts(2026, 10, 20, 0, 15), w, f"a_{w}", "Yes", 100.0, 40.0, 0.4),
            (_ts(2026, 10, 20, 0, 30), w, f"b_{w}", "Yes", 100.0, 40.0, 0.4),
        ]
    _pm_only_store(db, snaps, pos)
    prep = runner.Prep(runner.cmn.connect_ro(db), datetime(2026, 11, 25, tzinfo=UTC), tmp_path / "no_stage0")
    by = {e["account"]: e for e in prep.pm_events}
    assert set(by) == {"w1", "w9"} and by["w1"]["overall"] is True
    assert by["w9"]["overall"] is False and by["w9"]["tracked_prev_run"] is False


def _ev(ts: int, acc: str, tok: str, p: float = 0.4, entry: float = 0.4, first: bool = True) -> dict:
    return {
        "ts": ts, "account": acc, "market": tok, "side": "Yes", "size": 100.0, "value_usd": 100 * p, "entry_px": entry, "title": tok,
        "p_obs": p, "n_before": 1, "first_pair": first, "overall": True, "crypto": False, "tracked_prev_run": True,
        "tracked_prev_day": True, "tracked_before": True, "in_range": True, "fresh": smf.is_fresh_entry(p, entry),
    }  # fmt: skip


def _gm(tok: str, end: str, closed_time: str | None, win: str = "1", closed: bool = True) -> dict:
    return {
        "conditionId": f"c_{tok}", "clobTokenIds": json.dumps([tok, f"o_{tok}"]),
        "outcomePrices": json.dumps([win, "0" if win == "1" else "1"]), "closed": closed, "closedTime": closed_time, "endDate": end,
    }  # fmt: skip


def _h3(runner, tmp_path: Path, markets: list, events: list) -> dict:
    from types import SimpleNamespace  # noqa: PLC0415

    (tmp_path / "gamma").mkdir(exist_ok=True)
    (tmp_path / "gamma" / "batch_00000.json").write_text(json.dumps({"tokens": [], "markets": markets}))
    return runner.h3_block(SimpleNamespace(pm_events=events, as_of_s=_ts(2026, 11, 25)), tmp_path)


def test_runner_h3_main_sample_by_scheduled_end_and_first_pair(tmp_path):  # B2, B3 (a)
    runner = _load_runner()
    mk = [
        _gm("main", "2026-11-10T00:00:00Z", "2026-11-11T00:00:00Z"),
        _gm("early", "2026-12-31T00:00:00Z", "2026-11-10T00:00:00Z"),  # "X by 31.12" closed early (YES)
        _gm("late_seen", "2026-11-05T00:00:00Z", "2026-11-08T00:00:00Z"),  # event over, UMA pending
    ]
    ev = [
        _ev(_ts(2026, 11, 1), "w", "main"),
        _ev(_ts(2026, 11, 1), "w", "early"),
        _ev(_ts(2026, 11, 6), "w", "late_seen", p=0.96, entry=0.96),
        _ev(_ts(2026, 11, 2), "v", "main", first=False),  # a repeat of the pair
    ]
    res = _h3(runner, tmp_path, mk, ev)
    assert res["samples"] == {"first:main": 1, "first:end_after_as_of": 1, "first:seen_at_or_after_end": 1, "repeat:main": 1}
    assert res["overall"]["main"]["n"] == 1
    assert res["overall_end_after_as_of"]["edge"]["n"] == 1 and res["overall_repeats"]["edge"]["n"] == 1  # descriptive only


def test_runner_h3_unresolved_past_end_blocks_decision(tmp_path):  # B2: > 5 % unresolved -> no decision
    runner = _load_runner()
    mk = [_gm(f"r{i}", "2026-11-10T00:00:00Z", "2026-11-11T00:00:00Z") for i in range(9)]
    mk.append(_gm("u", "2026-11-10T00:00:00Z", None, closed=False))
    ev = [_ev(_ts(2026, 11, 1), f"w{i}", f"r{i}") for i in range(9)] + [_ev(_ts(2026, 11, 1), "w9", "u")]
    res = _h3(runner, tmp_path, mk, ev)
    assert res["overall"]["unresolved_share"] == pytest.approx(0.1)
    assert res["overall"]["status"] == "undecided" and res["overall"]["excluded"] is False


def test_runner_h3_stops_on_missing_gamma_markets(tmp_path):  # m3
    runner = _load_runner()
    mk = [_gm(f"r{i}", "2026-11-10T00:00:00Z", "2026-11-11T00:00:00Z") for i in range(9)]
    ev = [_ev(_ts(2026, 11, 1), f"w{i}", f"r{i}") for i in range(10)]  # r9 has no market: 10 % > 2 %
    res = _h3(runner, tmp_path, mk, ev)
    assert res.get("stopped") is True and "overall" not in res


def test_new_positions_repeat_is_not_first_pair():  # B3 (a)
    rows = [
        {"ts": 100, "account": "w", "market": "t1", "side": "Yes", "size": 10.0, "value_usd": 5.0, "entry_px": 0.5},
        {"ts": 200, "account": "w", "market": "t2", "side": "Yes", "size": 10.0, "value_usd": 5.0, "entry_px": 0.5},
        {"ts": 300, "account": "w", "market": "t2", "side": "closed", "size": 0.0},
        {"ts": 400, "account": "w", "market": "t2", "side": "Yes", "size": 10.0, "value_usd": 6.0, "entry_px": 0.5},
        {"ts": 500, "account": "w", "market": "t1", "side": "closed", "size": 0.0},
        {"ts": 600, "account": "w", "market": "t1", "side": "Yes", "size": 10.0, "value_usd": 6.0, "entry_px": 0.5},
    ]
    ev = smf.new_positions(rows, 50, 1000)
    assert [(e["ts"], e["market"], e["first_pair"]) for e in ev] == [(200, "t2", True), (400, "t2", False), (600, "t1", False)]


def test_new_positions_v2_baseline_per_wallet():  # m2: a wallet the first v2 run did not poll
    r = lambda ts, a, m, side="Yes": {"ts": ts, "account": a, "market": m, "side": side, "size": 10.0, "value_usd": 5.0}  # noqa: E731
    rows = [
        r(100, "k1", "t1"),
        r(100, "k2", "t1"),
        r(150, "k2", "t1", "closed"),
        r(1000, "k1", "t5"),
        r(1100, "k2", "t1"),
        r(1100, "k2", "t6"),
        r(1200, "k2", "t7"),
    ]
    ev = smf.new_positions(sorted(rows, key=lambda x: (x["ts"], x["account"])), 50, 2000, rebaseline_from=1000)
    assert [(e["account"], e["market"]) for e in ev] == [("k2", "t7")]  # k1 at 1000 and k2 at 1100 are v2 baselines


def test_fresh_entry_guard():  # B3 (b)
    assert smf.is_fresh_entry(0.50, 0.48) and smf.is_fresh_entry(0.50, 0.53)
    assert not smf.is_fresh_entry(0.80, 0.30) and not smf.is_fresh_entry(0.5, None) and not smf.is_fresh_entry(None, 0.5)


def _rows(n_markets: int, wallets: list, edge, fresh=lambda i, w: True) -> list:
    return [
        {"edge": edge(i, w), "cluster": f"m{i}", "account": w, "n_before": 1, "tracked_before": True, "fresh": fresh(i, w)}
        for i in range(n_markets)
        for w in wallets
    ]


def test_h3_evaluate_and_status_by_replication():  # B4, B3 (b), M1
    wallets = [f"w{k}" for k in range(10)]
    good = _rows(40, wallets, lambda i, w: 0.3 + 0.05 * ((i % 5) - 2))
    ev = smf.h3_evaluate(good, unresolved_share=0.0)
    assert ev["criterion_met"] is True and ev["level"] == pytest.approx(smf.DECISION_LEVEL)
    assert smf.h3_status(ev["criterion_met"], ev["decidable"], None, look=1) == "candidate"  # one slice: never 'confirmed'
    assert smf.h3_status(True, True, True, look=2) == "confirmed" and smf.h3_status(True, True, False, look=2) == "not_confirmed"
    assert smf.h3_status(True, False, None, look=1) == "undecided"
    # fresh-entry guard: the edge comes only from positions that grew in price before they were seen
    grown = _rows(40, wallets, lambda i, w: -0.2 if i % 2 == 0 else 0.8, fresh=lambda i, w: i % 2 == 0)
    ev2 = smf.h3_evaluate(grown, unresolved_share=0.0)
    assert ev2["registered"] is True and ev2["guards_ok"]["fresh_entry"] is False and ev2["criterion_met"] is False
    # concentration: one wallet with >= 25 % of positions
    big = good + [{**r, "account": "big"} for r in good if r["account"] in ("w0", "w1", "w2", "w3")]
    ev3 = smf.h3_evaluate(big, unresolved_share=0.0)
    assert ev3["max_wallet_share"] == pytest.approx(160 / 560) and ev3["concentration_ok"] is False and ev3["criterion_met"] is False
    assert ev3["guards"]["without_top5_wallets"]["n"] == 560 - 160 - 4 * 40
    assert smf.h3_evaluate(good, unresolved_share=0.06)["criterion_met"] is False


def test_h3_sample_by_scheduled_end():  # B2
    as_of = _ts(2026, 11, 25)
    end = _ts(2026, 11, 10)
    assert smf.h3_sample(_ts(2026, 11, 1), end, None, as_of) == "main"
    assert smf.h3_sample(_ts(2026, 11, 10), end, None, as_of) == "seen_at_or_after_end"
    assert smf.h3_sample(_ts(2026, 11, 1), _ts(2026, 11, 20), None, as_of) == "end_in_grace"
    assert smf.h3_sample(_ts(2026, 11, 1), _ts(2026, 12, 31), _ts(2026, 11, 2), as_of) == "end_after_as_of"
    assert smf.h3_sample(_ts(2026, 11, 3), _ts(2026, 12, 31), _ts(2026, 11, 2), as_of) == "closed_before_observation"
    assert smf.h3_sample(_ts(2026, 11, 1), end, None, as_of, end_after_s=_ts(2026, 11, 11)) == "earlier_slice"
    assert smf.h3_sample(_ts(2026, 11, 1), None, None, as_of) == "no_end_date"
    assert smf.gamma_end_ts({"endDate": "2026-11-10T00:00:00Z", "closedTime": "2026-11-02 00:00:00+00"}) == end


def test_h2_decision_coded():  # M3
    ic = {"mean": 0.05, "se": 0.01}
    sp = {"mean": 0.005, "se": 0.001}
    kw = {"look": 1, "earlier_decided": False}
    assert smf.h2_decision(ic, sp, gate=False, **kw)["verdict"] == "inconclusive"
    assert smf.h2_decision(ic, sp, gate=True, **kw)["verdict"] == "confirmed"  # 0.05 - 2.64 * 0.01 > 0
    assert smf.h2_decision({"mean": 0.05, "se": 0.02}, sp, gate=True, **kw)["verdict"] == "rejected"  # spread < 1 p.p.
    assert smf.h2_decision({"mean": 0.05, "se": 0.02}, {"mean": 0.02, "se": 0.005}, gate=True, **kw)["verdict"] == "confirmed"


# ---------------------------------------------------------------------------------------------------------------------
# Review round 2, R1 (2026-11-26, before any outcome): decisions only on the named decisive looks, priors only from the
# previous one. Each fails on the round-2 code (no DECISIVE_AS_OF / check_prior / look argument).

LOOK = {k + 1: s for k, s in enumerate(smf.DECISIVE_AS_OF)}


def test_decisive_looks_constant():
    assert LOOK == {1: _ts(2026, 11, 25), 2: _ts(2026, 12, 16), 3: _ts(2027, 1, 15)} and smf.DECISIVE_LOOKS == 3
    assert smf.decisive_look(_ts(2026, 12, 16)) == 2 and smf.decisive_look(_ts(2026, 12, 10)) is None
    assert smf.decisive_look(_ts(2026, 11, 25, 0, 1)) is None and smf.decisive_look(None) is None


def test_h3_status_and_h2_decision_off_decisive_looks():
    assert smf.h3_status(True, True, None, look=None) == "descriptive"  # e.g. as-of 2026-12-10
    assert smf.h3_status(True, True, True, look=None) == "descriptive"
    assert smf.h3_status(True, True, None, look=2) == "descriptive"  # look 2 without the look-1 prior: overlapping sample
    with pytest.raises(ValueError):
        smf.h3_status(True, True, True, look=1)
    ic, sp = {"mean": 0.05, "se": 0.01}, {"mean": 0.005, "se": 0.001}
    assert smf.h2_decision(ic, sp, gate=True, look=None, earlier_decided=False)["verdict"] == "descriptive"
    assert smf.h2_decision(ic, sp, gate=True, look=2, earlier_decided=None)["verdict"] == "descriptive"  # unknown history
    assert smf.h2_decision(ic, sp, gate=True, look=2, earlier_decided=True)["verdict"] == "descriptive"  # decided at look 1
    assert smf.h2_decision(ic, sp, gate=True, look=2, earlier_decided=False)["verdict"] == "confirmed"


def _prior(as_of_s, *, status="candidate", stopped=False, prior_as_of_s=None, fp=None) -> dict:
    return {"as_of_s": as_of_s, "prior_as_of_s": prior_as_of_s, "stopped": stopped, "status": {"overall": status, "crypto": status},
            "registration": smf.registration_fingerprint() if fp is None else fp, "overall": True, "crypto": True}  # fmt: skip


def test_check_prior_rules():
    fp = smf.registration_fingerprint()
    assert smf.check_prior(None, _ts(2026, 12, 10), fp) is None  # no prior: the status rule handles it
    assert smf.check_prior(_prior(LOOK[1]), LOOK[2], fp) is None  # the valid pair (1 -> 2)
    assert smf.check_prior(_prior(_ts(2026, 11, 15)), LOOK[1], fp) is not None  # a non-decisive early look
    assert smf.check_prior(_prior(LOOK[1]), _ts(2026, 12, 10), fp) is not None  # current as-of not decisive
    assert smf.check_prior(_prior(LOOK[1]), LOOK[3], fp) is not None  # not neighbours (1 -> 3)
    assert smf.check_prior(_prior(LOOK[1]), LOOK[1], fp) is not None  # the same look
    assert smf.check_prior(_prior(LOOK[1], stopped=True), LOOK[2], fp) is not None  # stopped by the Gamma check
    assert smf.check_prior(_prior(LOOK[1], status="descriptive"), LOOK[2], fp) is not None
    assert smf.check_prior(_prior(LOOK[1], status=None), LOOK[2], fp) is not None
    other = smf.registration_fingerprint({"h3": [0.0]})
    assert smf.check_prior(_prior(LOOK[1], fp=other), LOOK[2], fp) is not None  # different registered parameters
    assert smf.check_prior(_prior(LOOK[2]), LOOK[3], fp) is not None  # look-2 prior run without its own prior
    assert smf.check_prior(_prior(LOOK[2], prior_as_of_s=LOOK[1], status="not_confirmed"), LOOK[3], fp) is None


def test_early_close_m7():
    end = _ts(2026, 11, 15)
    assert smf.early_close(end, end - 86400 - 1) and not smf.early_close(end, end - 86400) and not smf.early_close(end, None)
    assert not smf.early_close(None, end)


def _h3_at(runner, tmp_path: Path, markets: list, events: list, as_of_s: int, prior=None) -> dict:
    from types import SimpleNamespace  # noqa: PLC0415

    (tmp_path / "gamma").mkdir(exist_ok=True)
    (tmp_path / "gamma" / "batch_00000.json").write_text(json.dumps({"tokens": [], "markets": markets}))
    return runner.h3_block(SimpleNamespace(pm_events=events, as_of_s=as_of_s), tmp_path, prior)


def _replication_case():
    """40 markets ending 2026-12-01 (inside the (1 -> 2) window), 10 wallets, all won: the criterion holds."""
    mk = [_gm(f"t{i}", "2026-12-01T00:00:00Z", "2026-12-02T00:00:00Z") for i in range(40)]
    ev = [
        _ev(_ts(2026, 11, 20), f"w{k}", f"t{i}", p=(0.3, 0.4, 0.5)[(i + k) % 3], entry=(0.3, 0.4, 0.5)[(i + k) % 3])
        for i in range(40)
        for k in range(10)
    ]
    return mk, ev


def test_runner_r1_priors_and_looks(tmp_path):
    runner = _load_runner()
    mk, ev = _replication_case()
    good = {**_prior(LOOK[1]), "registration": runner.registration(), "h2_decided_at_s": None}
    res = _h3_at(runner, tmp_path, mk, ev, LOOK[2], good)
    assert res["overall"]["criterion_met"] is True and res["overall"]["status"] == "confirmed"  # the valid pair still works
    assert _h3_at(runner, tmp_path, mk, ev, _ts(2026, 12, 10))["overall"]["status"] == "descriptive"  # not decisive
    assert _h3_at(runner, tmp_path, mk, ev, LOOK[2])["overall"]["status"] == "descriptive"  # look 2 without a prior
    for bad, as_of in (
        ({**good, "as_of_s": _ts(2026, 11, 15)}, LOOK[1]),  # the review's scenario: never 'confirmed' on slice 1
        ({**good, "as_of_s": _ts(2026, 11, 15)}, LOOK[2]),
        (good, LOOK[3]),  # non-neighbouring pair 11-25 -> 01-15
        ({**good, "stopped": True}, LOOK[2]),
        (good, _ts(2026, 12, 10)),
    ):
        with pytest.raises(ValueError, match="refused"):
            _h3_at(runner, tmp_path, mk, ev, as_of, bad)
    stopped_body = {"as_of_s": LOOK[1], "registration": runner.registration(), "h3": {"data_quality": {}, "stopped": True}}
    assert runner.prior_from_body(stopped_body)["stopped"] is True
    with pytest.raises(ValueError, match="stopped"):
        runner.accepted_prior(runner.prior_from_body(stopped_body), LOOK[2])


def test_runner_m7_early_close_row_is_descriptive(tmp_path):
    runner = _load_runner()
    mk = [_gm("a", "2026-11-15T00:00:00Z", "2026-11-05T00:00:00Z"), _gm("b", "2026-11-10T00:00:00Z", "2026-11-10T02:00:00Z")]
    ev = [_ev(_ts(2026, 11, 1), "w", "a"), _ev(_ts(2026, 11, 1), "v", "b")]
    res = _h3_at(runner, tmp_path, mk, ev, LOOK[1])
    assert res["overall_early_close"] == {"markets": 1, "of": 2} and res["overall"]["main"]["n"] == 2  # counted, not dropped


def test_runner_full_json_round_trips_as_a_prior(tmp_path):
    runner = _load_runner()
    db = tmp_path / "store.sqlite"
    _stub_store(db)
    cache = tmp_path / "cache"
    _stub_cache(cache)
    prep = runner.Prep(runner.cmn.connect_ro(db), datetime(2026, 11, 25, tzinfo=UTC), tmp_path / "no_stage0")
    res = json.loads(json.dumps(runner.full(prep, cache, seed=1, no_fetch=True), default=str))
    assert res["look"] == 1 and res["prior_as_of_s"] is None and res["h1"]["decision"] == "not_coded"
    prior = runner.prior_from_body(res)
    assert runner.accepted_prior(prior, LOOK[2]) is prior and prior["h2_decided_at_s"] in (None, LOOK[1])
    with pytest.raises(ValueError, match="refused"):
        runner.full(prep, cache, seed=1, no_fetch=True, prior={**prior, "as_of_s": _ts(2026, 11, 15)})
