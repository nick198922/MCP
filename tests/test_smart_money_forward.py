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


# ---------------------------------------------------------------------------------------------------------------------
# Before slice 2 (critic reviews 2026-11-26: slice-1 results, findings 2-3; registration round 2, m8). Rules unchanged.


def _lb(av: float, day, week, month) -> dict:
    return {"account_value": av, "pnl_day": day, "pnl_week": week, "pnl_month": month}


def test_leaderboard_zero_quality_counts_impossible_combinations():
    snap = {
        "a": _lb(1e6, 5.0, 10.0, 0.0),  # month 0, week != 0: impossible
        "b": _lb(1e6, 3.0, 0.0, 7.0),  # week 0, day != 0: impossible
        "c": _lb(1e6, 3.0, 0.0, 0.0),  # week 0, day != 0 (month 0 with week 0 is possible)
        "d": _lb(1e6, 0.0, 0.0, 0.0),  # all zero: possible (inactive)
        "e": _lb(1e6, None, None, 0.0),  # month 0, week unknown: not counted as a combination
        "f": _lb(1e6, 1.0, 2.0, 3.0),
        "g": _lb(4e5, 5.0, 10.0, 0.0),  # below the $500k floor
        "h": _lb(None, 5.0, 10.0, 0.0),
        "i": _lb(1e6, 1.0, float("nan"), 0.0),  # non-finite week: neither zero nor non-zero
    }
    q = smf.leaderboard_zero_quality(snap)
    assert q == {"n": 7, "month0": 5, "week0": 3, "month0_week_nz": 1, "week0_day_nz": 2}
    assert smf.leaderboard_zero_quality(snap, min_value=0.0)["month0_week_nz"] == 2  # + g; h has no account value
    assert smf.leaderboard_zero_quality({}) == {"n": 0, "month0": 0, "week0": 0, "month0_week_nz": 0, "week0_day_nz": 0}


def test_runner_hl_zero_rows_per_day_no_lookahead_and_before_outcomes(tmp_path, capsys):
    from types import SimpleNamespace  # noqa: PLC0415

    runner = _load_runner()
    d1, d2 = date(2026, 10, 16), date(2026, 10, 17)
    prep = SimpleNamespace(hl={d1: {"a": _lb(1e6, 5.0, 10.0, 0.0)}}, h1=[(d1, date(2026, 11, 15))], h1w=[(d1, date(2026, 10, 23))])
    early = runner.hl_zero_block(prep)
    prep.hl[d2] = {"a": _lb(1e6, 3.0, 0.0, 7.0), "b": _lb(1e6, 0.0, 0.0, 0.0)}  # a later day changes nothing before it
    rows = runner.hl_zero_block(prep)
    assert (
        rows[0]
        == early[0]
        == {"d": "2026-10-16", "formation": "H1, H1w", "n": 1, "month0": 1, "week0": 0, "month0_week_nz": 1, "week0_day_nz": 0}
    )
    assert rows[1]["week0_day_nz"] == 1 and rows[1]["formation"] == "" and rows[1]["n"] == 2
    db = tmp_path / "store.sqlite"
    _stub_store(db)
    cache = tmp_path / "cache"
    _stub_cache(cache)
    p = runner.Prep(runner.cmn.connect_ro(db), datetime(2026, 11, 25, tzinfo=UTC), tmp_path / "no_stage0")
    capsys.readouterr()
    res = runner.full(p, cache, seed=1, no_fetch=True)
    text = capsys.readouterr().out
    assert len(res["hl_zero_quality"]) == 40 and res["hl_zero_quality"][0]["month0"] == 1  # 10-16 .. 11-24 (11-25 00:05 > as-of)
    assert text.index("Якість нулів лідерборду HL") < text.index("план запитів") < text.index("### H1:")  # before the fetch / outcomes


class _FlakyGamma(_FakeGamma):
    """Raises a ProviderError (HTTP 500) on the first ``fail[token]`` calls whose batch contains the token."""

    def __init__(self, open_markets: dict, fail: dict) -> None:
        super().__init__(open_markets, {})
        self.fail = dict(fail)

    async def get_json(self, path, params, ttl, tool, args):
        from tradedesk.core.errors import ProviderError  # noqa: PLC0415

        for t in params["clob_token_ids"]:
            if self.fail.get(t, 0) > 0:
                self.fail[t] -= 1
                self.calls.append(dict(params))
                raise ProviderError("polymarket_gamma", "upstream", "HTTP 500")
        return await super().get_json(path, params, ttl, tool, args)


def _gamma_rt(g):
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(polymarket=SimpleNamespace(gamma=g))


def test_fetch_gamma_retries_a_failed_batch_in_the_same_run(tmp_path, capsys):
    import asyncio  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415

    runner = _load_runner()
    runner.GAMMA_BATCH, runner.GAMMA_RETRY_PAUSE_S = 1, 0.0
    m = lambda t: {"clobTokenIds": json.dumps([t]), "closed": False}  # noqa: E731
    g = _FlakyGamma({t: m(t) for t in "abc"}, {"b": 1})
    failed = Counter()
    asyncio.run(runner.fetch_gamma(_gamma_rt(g), tmp_path, ["a", "b", "c"], failed))
    assert sorted(runner.gamma_map(tmp_path)) == ["a", "b", "c"]  # b recovered by retry round 1
    assert failed["gamma"] == 0 and failed["gamma_attempt_errors"] == 1
    assert [c["clob_token_ids"] for c in g.calls] == [["a"], ["b"], ["c"], ["b"]]
    assert "retry round 1/2" in capsys.readouterr().out


def test_fetch_gamma_retry_is_bounded_and_reports_what_still_failed(tmp_path, capsys):
    import asyncio  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415

    runner = _load_runner()
    runner.GAMMA_BATCH, runner.GAMMA_RETRY_PAUSE_S = 1, 0.0
    m = lambda t: {"clobTokenIds": json.dumps([t]), "closed": False}  # noqa: E731
    g = _FlakyGamma({t: m(t) for t in "abc"}, {"b": 10**6})  # b always fails
    failed = Counter()
    asyncio.run(runner.fetch_gamma(_gamma_rt(g), tmp_path, ["a", "b", "c"], failed))
    assert sum(c["clob_token_ids"] == ["b"] for c in g.calls) == 1 + runner.GAMMA_RETRY_ROUNDS  # no endless loop
    assert failed["gamma"] == 1 and failed["gamma_attempt_errors"] == 1 + runner.GAMMA_RETRY_ROUNDS
    assert not (tmp_path / "gamma" / "batch_00001.json").exists() and sorted(runner.gamma_map(tmp_path)) == ["a", "c"]
    assert "still failed after 2 retry rounds: 1 batches (1)" in capsys.readouterr().out
    n = len(g.calls)
    g.fail["b"] = 0
    asyncio.run(runner.fetch_gamma(_gamma_rt(g), tmp_path, ["a", "b", "c"], Counter()))
    assert [c["clob_token_ids"] for c in g.calls[n:]] == [["b"]]  # a later attempt asks only the missing batch


def test_free_out_never_overwrites(tmp_path):
    runner = _load_runner()
    out = tmp_path / "full.md"
    assert runner.free_out(out) == out
    out.write_text("attempt 1")
    assert runner.free_out(out) == tmp_path / "full.2.md"
    out.unlink()
    (tmp_path / "full.json").write_text("{}")  # the JSON alone also blocks the name
    assert runner.free_out(out) == tmp_path / "full.2.md"
    (tmp_path / "full.2.md").write_text("attempt 2")
    assert runner.free_out(out) == tmp_path / "full.3.md"
    (tmp_path / "rep.json").write_text("{}")  # slice-2 prep review, finding 3: no .md suffix used to loop forever
    for bad in ("rep", "rep.json", "rep.txt"):
        with pytest.raises(ValueError, match=r"must end in \.md"):
            runner.free_out(tmp_path / bad)


def test_full_json_records_what_the_attempt_could_not_fetch(tmp_path, monkeypatch):
    """Slice-2 prep review, finding 6: ``fetch_failed`` is descriptive (None = --no-fetch replay); the JSON is still
    accepted as a prior, a replay's JSON is not."""
    runner = _load_runner()
    db = tmp_path / "store.sqlite"
    _stub_store(db)
    cache = tmp_path / "cache"
    _stub_cache(cache)
    prep = runner.Prep(runner.cmn.connect_ro(db), datetime(2026, 11, 25, tzinfo=UTC), tmp_path / "no_stage0")

    async def fake_fetch(prep, cache, plan):
        return {"gamma": 2, "gamma_attempt_errors": 6}

    monkeypatch.setattr(runner, "fetch", fake_fetch)
    fetched = json.loads(json.dumps(runner.full(prep, cache, seed=1, no_fetch=False), default=str))
    replay = json.loads(json.dumps(runner.full(prep, cache, seed=1, no_fetch=True), default=str))
    assert fetched["fetch_failed"] == {"gamma": 2, "gamma_attempt_errors": 6} and replay["fetch_failed"] is None
    assert {k: v for k, v in fetched.items() if k != "fetch_failed"} == {k: v for k, v in replay.items() if k != "fetch_failed"}
    (tmp_path / "full.json").write_text(json.dumps(fetched))
    (tmp_path / "replay.json").write_text(json.dumps(replay))
    prior = runner.load_prior(str(tmp_path / "full.json"))
    assert prior == runner.prior_from_body(replay) and runner.accepted_prior(prior, LOOK[2]) is prior
    with pytest.raises(ValueError, match="replay"):
        runner.load_prior(str(tmp_path / "replay.json"))


def test_decisive_attempt_is_the_first_json_whose_h3_is_not_stopped(tmp_path):
    """Card rule «порядок спроб»: a new full attempt is allowed only while every earlier one of the series crashed
    before its JSON or was stopped by the Gamma check; the first one that is not stopped is decisive."""
    runner = _load_runner()
    out, a = tmp_path / "full.md", LOOK[2]
    ok = {"as_of_s": a, "fetch_failed": {}, "h3": {"overall": {"status": "not_confirmed"}, "crypto": {"status": "not_confirmed"}}}
    assert runner.decisive_attempt(out, a) is None  # no attempt yet
    (tmp_path / "full.md").write_text("attempt 1 crashed before its JSON")
    (tmp_path / "coverage.json").write_text(json.dumps({"as_of_s": a}))
    assert runner.decisive_attempt(out, a) is None
    (tmp_path / "full.2.json").write_text(json.dumps({**ok, "h3": {"stopped": True}}))  # stopped by the Gamma check
    (tmp_path / "full.3.json").write_text(json.dumps({**ok, "fetch_failed": None}))  # a replay is not an attempt
    (tmp_path / "full.x.json").write_text(json.dumps(ok))  # not in the series
    (tmp_path / "full.4.json").write_text(json.dumps({**ok, "as_of_s": LOOK[1]}))  # another as-of
    assert runner.decisive_attempt(out, a) is None
    (tmp_path / "full.10.json").write_text(json.dumps(ok))
    (tmp_path / "full.5.json").write_text(json.dumps(ok))
    assert runner.decisive_attempt(out, a) == tmp_path / "full.5.json"  # numeric order, not lexicographic
    legacy = {k: v for k, v in ok.items() if k != "fetch_failed"}  # slice 1's full.json predates the key
    (tmp_path / "full.json").write_text(json.dumps(legacy))
    assert runner.decisive_attempt(out, a) == tmp_path / "full.json"


def _head(as_of_s: int, stage: str = "full", iso: str | None = None) -> str:
    iso = iso or datetime.fromtimestamp(as_of_s, UTC).isoformat()
    return f"# smart-money slice 1, stage {stage}, as-of {iso}, seed 1\nout: x\n"


def _seen(as_of_s: int, tail: str = "") -> str:
    """A report that reached the line before the first outcome (an attempt by the amendment 2026-11-27)."""
    return _head(as_of_s) + "fetch done: failed {}\n\n## Зріз: метрики на 2026-12-16 00:00 UTC\n" + tail


def test_series_attempts_counts_reports_that_reached_the_outcomes(tmp_path):
    """Amendment 2026-11-27 (review blocker 2): a report counts once it holds the ``## Зріз:`` line, printed after the
    fetch and before ``### H1`` (the first outcome); a crash before it uses up no attempt."""
    runner = _load_runner()
    out, a = tmp_path / "full.md", LOOK[2]
    assert runner.series_attempts(out, a) == []
    (tmp_path / "full.md").write_text(_seen(a, "### H1 / H1w: aborted\n"))  # aborted after outcomes, no JSON: counted
    (tmp_path / "full.2.md").write_text("")  # empty: crash before the first line, not counted
    (tmp_path / "full.3.md").write_text(_head(a) + "план запитів: …\n  gamma: 10/90 batches\n")  # crash in the fetch
    (tmp_path / "full.4.md").write_bytes(b"\xff\xfe broken")  # unreadable: counted (conservative)
    (tmp_path / "full.5.md").write_text(_seen(LOOK[1]))  # another as-of
    (tmp_path / "full.6.md").write_text(_seen(a).replace("stage full", "stage coverage"))  # another stage
    (tmp_path / "full.x.md").write_text(_seen(a))  # not in the series
    (tmp_path / "full-s2.md").write_text(_seen(a))  # series 2 is not series 1
    (tmp_path / "full.10.md").write_text(_seen(a))
    # a fractional header of the same whole second (written before the 27.11 refusal) is counted, not skipped
    (tmp_path / "full.11.md").write_text(_seen(a).replace("00:00:00+00:00", "00:00:00.500000+00:00", 1))
    want = ["full.md", "full.4.md", "full.10.md", "full.11.md"]
    assert runner.series_attempts(out, a) == [tmp_path / n for n in want]
    assert runner.series_attempts(tmp_path / "full-s2.md", a) == [tmp_path / "full-s2.md"]
    assert runner.MAX_ATTEMPTS == 3
    assert runner.SLICE_HEAD == "## Зріз: "


def test_full_stage_prints_the_attempt_line_before_any_outcome():
    """Blocker 2: ``full()`` emits ``## Зріз:`` before it calls the first outcome block (``h1_block``)."""
    runner = _load_runner()
    import inspect  # noqa: PLC0415

    src = inspect.getsource(runner.full)
    assert src.index("SLICE_HEAD") < src.index("h1_block(") < src.index("h2_block(") < src.index("h3_block(")
    assert src.index("fetch(prep") < src.index("SLICE_HEAD")


def test_emit_writes_the_report_before_the_screen(tmp_path, monkeypatch, capsys):
    """Blocker 2: no line reaches the screen before the report file (which counts the attempt) holds it."""
    runner = _load_runner()
    path = tmp_path / "full.md"
    seen: list[str] = []
    real_print = print

    def spy(*args, **kw):
        seen.append(path.read_text(encoding="utf-8"))
        real_print(*args, **kw)

    monkeypatch.setattr(runner, "print", spy, raising=False)
    with open(path, "x", encoding="utf-8") as fh:
        monkeypatch.setitem(runner.OUT, "fh", fh)
        runner.emit("## Зріз: метрики")
    assert seen == ["## Зріз: метрики\n"]
    assert capsys.readouterr().out == "## Зріз: метрики\n"


def test_registration_fingerprint_is_the_one_slice_1_was_run_with():
    """Review 2026-11-27, remark 2: sha256 of the sorted registration equals that of the ``registration`` stored in
    slice 1's ``full.json`` (sha256 564a9eef…c0bd), so any drift that would break slice 1 as a prior breaks CI."""
    import hashlib  # noqa: PLC0415

    runner = _load_runner()
    got = hashlib.sha256(json.dumps(runner.registration(), sort_keys=True).encode()).hexdigest()
    assert got == "50025b124d73248969691560b4995ad409be812ee221796c0c4415a1101fd843"


class _StoreReached(Exception):
    pass


def _main(runner, monkeypatch, *args: str) -> None:
    """Run the runner's ``main()`` with ``args``; the store is never opened (no live database, no network)."""

    def no_store(db, cache):
        raise _StoreReached

    monkeypatch.setattr(runner, "open_store", no_store)
    monkeypatch.setattr("sys.argv", ["verify_smart_money_slice1.py", *args])
    runner.main()


def test_main_refuses_attempts_the_card_rule_does_not_allow(tmp_path, monkeypatch):
    """R2-2: the refusals of «порядок спроб» inside ``main()`` happen before any file is written or data is read."""
    runner = _load_runner()
    a = LOOK[2]
    iso = datetime.fromtimestamp(a, UTC).isoformat()
    full = ("--stage", "full", "--as-of", iso, "--cache-dir", str(tmp_path))
    stopped = {"as_of_s": a, "fetch_failed": {"gamma": 9}, "h3": {"stopped": True}}

    # --no-fetch at a decisive as-of without --out: a replay, not an attempt
    with pytest.raises(SystemExit, match="--no-fetch at a decisive as-of"):
        _main(runner, monkeypatch, *full, "--no-fetch")
    assert list(tmp_path.glob("full*")) == []

    # attempt 1 aborted after the outcomes (no JSON), attempt 2 stopped by the Gamma check: attempt 3 is allowed
    (tmp_path / "full.md").write_text(_seen(a, "### H1 / H1w: …\n"))
    (tmp_path / "full.2.md").write_text(_seen(a))
    (tmp_path / "full.2.json").write_text(json.dumps(stopped))
    with pytest.raises(_StoreReached):
        _main(runner, monkeypatch, *full)
    third = (tmp_path / "full.3.md").read_text()
    assert third.startswith(_head(a).partition(" seed")[0]) and "\ncode: " in third
    assert not (tmp_path / "full.3.json").exists()
    # the third reached the store and crashed there, before any outcome: no attempt used, the next one is allowed
    assert runner.series_attempts(tmp_path / "full.md", a) == [tmp_path / "full.md", tmp_path / "full.2.md"]
    (tmp_path / "full.3.md").write_text(_seen(a))  # now as if it had crashed after the outcomes
    with pytest.raises(SystemExit, match="attempt cap reached: 3 of 3"):
        _main(runner, monkeypatch, *full)
    assert not (tmp_path / "full.4.md").exists()

    # a --no-fetch replay outside the series is still allowed after the cap
    (tmp_path / "tmp").mkdir()
    with pytest.raises(_StoreReached):
        _main(runner, monkeypatch, *full, "--no-fetch", "--out", str(tmp_path / "tmp" / "replay.md"))

    # a decisive attempt already written: a new attempt is refused (before the cap check)
    dec = tmp_path / "dec"
    dec.mkdir()
    (dec / "full.md").write_text(_seen(a))
    (dec / "full.json").write_text(json.dumps({**stopped, "h3": {"overall": {"status": "not_confirmed"}}}))
    with pytest.raises(SystemExit, match="decisive attempt of this as-of is already written"):
        _main(runner, monkeypatch, "--stage", "full", "--as-of", iso, "--cache-dir", str(dec))
    assert sorted(p.name for p in dec.iterdir()) == ["full.json", "full.md"]
    # ... and series 2 is refused there too (series 1 has a decisive attempt)
    with pytest.raises(SystemExit, match="--series 2 is allowed only after"):
        _main(runner, monkeypatch, "--stage", "full", "--as-of", iso, "--cache-dir", str(dec), "--series", "2")

    # a non-decisive as-of is descriptive: neither rule applies (closed series of the decisive as-of in tmp_path)
    with pytest.raises(_StoreReached):
        _main(runner, monkeypatch, *full[:3], datetime.fromtimestamp(a + 60, UTC).isoformat(), *full[4:])

    # review remark 3: the registered coverage -> full flow, a clean first full next to coverage.md / coverage.json
    cov = tmp_path / "cov"
    cov.mkdir()
    (cov / "coverage.md").write_text(_seen(a).replace("stage full", "stage coverage"))
    (cov / "coverage.json").write_text("{}")
    with pytest.raises(_StoreReached):
        _main(runner, monkeypatch, "--stage", "full", "--as-of", iso, "--cache-dir", str(cov))
    assert (cov / "full.md").exists()


@pytest.mark.parametrize("as_of", ["2026-12-16T00:00:00.5Z", "2026-12-16T00:00:00.000001+00:00", "2026-12-16T02:00:00.25+02:00"])
def test_main_refuses_a_fractional_as_of_before_any_write(tmp_path, monkeypatch, as_of):
    """Review 2026-11-27, blocker 5: ``…00.5Z`` is decisive by its whole second but its header was not counted."""
    runner = _load_runner()
    cache = tmp_path / "c"
    for stage in ("full", "coverage"):
        with pytest.raises(SystemExit, match="whole second"):
            _main(runner, monkeypatch, "--stage", stage, "--as-of", as_of, "--cache-dir", str(cache))
    assert not cache.exists()


def _attempts(runner, monkeypatch, cache: Path, as_of_s: int, modes: list[str], *extra: str) -> list[str]:
    """Run ``main()`` once per mode with a fake store and a fake ``full()`` that prints what the real one prints, in
    the same order: the fetch lines, then ``## Зріз:``, then ``### H1`` (the first outcome); ``crash_fetch`` dies in
    the fetch, ``crash_after`` after ``### H1``, ``stopped`` / ``clean`` return a JSON body. Returns each outcome."""

    def fake_full(prep, cache_, seed, no_fetch, prior=None):
        mode = modes_left.pop(0)
        runner.emit("план запитів: portfolio 1, candles 1, candles1h 1, gamma 1")
        if mode == "crash_fetch":
            raise RuntimeError("OOM in the fetch")
        runner.emit("fetch done: failed {}")
        runner.emit(f"{runner.SLICE_HEAD}метрики на … UTC")
        runner.emit("### H1 / H1w: …")
        if mode == "crash_after":
            raise RuntimeError("killed after H1")
        h3 = {"stopped": True} if mode == "stopped" else {"overall": {"status": "not_confirmed"}}
        return {"as_of_s": as_of_s, "fetch_failed": {"gamma": 0}, "h3": h3}

    modes_left = list(modes)
    monkeypatch.setattr(runner, "open_store", lambda db, c: None)
    monkeypatch.setattr(runner, "Prep", lambda conn, as_of, stage0: None)
    monkeypatch.setattr(runner, "full", fake_full)
    iso = datetime.fromtimestamp(as_of_s, UTC).isoformat()
    got = []
    for _ in modes:
        monkeypatch.setattr("sys.argv", ["v.py", "--stage", "full", "--as-of", iso, "--cache-dir", str(cache), *extra])
        try:
            runner.main()
            got.append("ok")
        except RuntimeError:
            got.append("crash")
        except SystemExit as exc:
            got.append(f"refused: {str(exc)[:40]}")
            modes_left.pop(0)
    return got


def test_stopped_crashed_and_clean_attempts_behave_as_the_card_says(tmp_path, monkeypatch):
    """Amendment 2026-11-27 after review (blockers 1, 2): crashes before ``## Зріз:`` use up no attempt; crashes after
    it and stopped attempts do; 3 counted attempts close series 1; then exactly one series 2 (``full-s2*``) with the
    same cap; a clean attempt is decisive and ends its series."""
    runner = _load_runner()
    a, cache = LOOK[2], tmp_path / "slice2"
    got = _attempts(runner, monkeypatch, cache, a, ["crash_fetch", "crash_fetch", "stopped", "crash_after", "stopped", "x"])
    assert got[:5] == ["crash", "crash", "ok", "crash", "ok"] and got[5].startswith("refused: attempt cap reached: 3 of 3")
    s1 = cache / "full.md"
    assert [p.name for p in runner.series_attempts(s1, a)] == ["full.3.md", "full.4.md", "full.5.md"]
    assert sorted(p.name for p in cache.glob("full*.json")) == ["full.3.json", "full.5.json"]  # п. 9: data from full.5
    assert runner.decisive_attempt(s1, a) is None
    assert not (cache / "full.6.md").exists()
    for f in ("full.md", "full.2.md"):  # crashed in the fetch: the file holds no outcome line
        assert runner.SLICE_HEAD not in (cache / f).read_text()

    # series 2 (after the one Gamma fix): a pre-outcome crash is free, a stopped one counts, a clean one is decisive
    got = _attempts(runner, monkeypatch, cache, a, ["crash_fetch", "stopped", "clean", "x"], "--series", "2")
    assert got[:3] == ["crash", "ok", "ok"] and got[3].startswith("refused: the decisive attempt of this as-of")
    s2 = cache / "full-s2.md"
    assert runner.decisive_attempt(s2, a) == cache / "full-s2.3.json"
    assert [p.name for p in runner.series_attempts(s2, a)] == ["full-s2.2.md", "full-s2.3.md"]
    # series 1 stays closed, and a third series does not exist
    assert _attempts(runner, monkeypatch, cache, a, ["x"])[0].startswith("refused: attempt cap reached")
    with pytest.raises(SystemExit):
        _main(
            runner,
            monkeypatch,
            "--stage",
            "full",
            "--as-of",
            datetime.fromtimestamp(a, UTC).isoformat(),
            "--cache-dir",
            str(cache),
            "--series",
            "3",
        )
    # within a series every attempt prints the same code hash
    codes = {
        next(line for line in (cache / f).read_text().splitlines() if line.startswith("code: "))
        for f in ("full.3.md", "full.5.md", "full-s2.3.md")
    }
    assert len(codes) == 1


def test_series_2_only_after_series_1_closed_and_only_as_attempts(tmp_path, monkeypatch):
    runner = _load_runner()
    a = LOOK[2]
    iso = datetime.fromtimestamp(a, UTC).isoformat()
    cache = tmp_path / "c"
    s2 = ("--stage", "full", "--as-of", iso, "--cache-dir", str(cache), "--series", "2")
    with pytest.raises(SystemExit, match="--series 2 is allowed only after series 1 counted 3"):
        _main(runner, monkeypatch, *s2)  # series 1 not started
    cache.mkdir(exist_ok=True)
    for f in ("full.md", "full.2.md"):
        (cache / f).write_text(_seen(a))
    with pytest.raises(SystemExit, match="--series 2 is allowed only after series 1 counted 3"):
        _main(runner, monkeypatch, *s2)  # series 1 still open (2 of 3)
    for bad in (("--no-fetch",), ("--out", str(tmp_path / "x.md"))):
        with pytest.raises(SystemExit, match="--series 2 is an attempt series"):
            _main(runner, monkeypatch, *s2, *bad)
    with pytest.raises(SystemExit, match="--series 2 is an attempt series"):
        _main(runner, monkeypatch, "--stage", "coverage", *s2[2:])
    with pytest.raises(SystemExit, match="--series 2 is an attempt series"):  # not a decisive as-of
        _main(runner, monkeypatch, "--stage", "full", "--as-of", datetime.fromtimestamp(a + 60, UTC).isoformat(), *s2[4:])
    (cache / "full.3.md").write_text(_seen(a))
    with pytest.raises(_StoreReached):
        _main(runner, monkeypatch, *s2)
    assert (cache / "full-s2.md").exists()
    assert sorted(p.name for p in cache.iterdir()) == ["full-s2.md", "full.2.md", "full.3.md", "full.md"]


def _h2_case(runner, cache: Path, as_of_s: int):
    """Synthetic H2 with a perfect daily IC (coin j's flow and its excess return both grow with j): gate passed and
    the registered criterion met, so only the look / prior logic decides the verdict."""
    from datetime import timedelta  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    coins = [f"C{j}" for j in range(12)]
    (cache / "candles").mkdir(parents=True, exist_ok=True)
    for j, c in enumerate(["BTC", *coins]):
        g = 0.0 if c == "BTC" else 0.001 * j
        rows = [{"t": _ts(2026, 10, 15) * 1000 + k * 86_400_000, "c": 100.0 * (1 + g) ** k} for k in range(100)]
        (cache / "candles" / f"{c}.json").write_text(json.dumps(rows))
    as_of = datetime.fromtimestamp(as_of_s, UTC)
    days = [runner.H2_FIRST + timedelta(days=k) for k in range((as_of.date() - runner.H2_FIRST).days)]
    sig = {c: float(j + 1) for j, c in enumerate(coins)}
    h2 = {d: {"signal": sig, "signal_early": sig, "skip": None} for d in days}
    return SimpleNamespace(h2=h2, h2_days=days, as_of_s=as_of_s, as_of_day=as_of.date())


def test_runner_m8_h2_decided_on_an_earlier_look_is_descriptive_and_carries_the_date(tmp_path, capsys):
    runner = _load_runner()
    prep2 = _h2_case(runner, tmp_path, LOOK[2])
    undecided = {**_prior(LOOK[1]), "registration": runner.registration(), "h2_decided_at_s": None}
    dec = runner.h2_block(prep2, tmp_path, undecided)["decision"]
    assert dec["verdict"] == "confirmed" and dec["decided_at_s"] == LOOK[2]  # control: the first decision is at look 2
    decided = {**undecided, "h2_decided_at_s": LOOK[1]}
    capsys.readouterr()
    dec = runner.h2_block(prep2, tmp_path, decided)["decision"]
    assert dec["verdict"] == "descriptive" and dec["decided_at_s"] == LOOK[1] and dec["ic_ok"] is True  # m8
    assert "рішення вже ухвалено на зрізі 2026-11-25" in capsys.readouterr().out
    dec = runner.h2_block(prep2, tmp_path, None)["decision"]
    assert dec["verdict"] == "descriptive" and dec["decided_at_s"] is None  # look 2 without a prior: unknown history
    prep3 = _h2_case(runner, tmp_path, LOOK[3])
    carried = {**decided, "as_of_s": LOOK[2], "prior_as_of_s": LOOK[1]}  # slice 2's JSON carried look 1's date
    dec = runner.h2_block(prep3, tmp_path, carried)["decision"]
    assert dec["verdict"] == "descriptive" and dec["decided_at_s"] == LOOK[1]
    body = {"as_of_s": LOOK[1], "registration": runner.registration(), "h2": {"decision": {"verdict": "confirmed", "decided_at_s": LOOK[1]}},
            "h3": {"overall": {"status": "candidate"}, "crypto": {"status": "not_candidate"}}}  # fmt: skip
    prior = runner.accepted_prior(runner.prior_from_body(body), LOOK[2])  # the full-JSON route of the same date
    assert prior["h2_decided_at_s"] == LOOK[1]
    assert runner.h2_block(prep2, tmp_path, prior)["decision"]["verdict"] == "descriptive"
