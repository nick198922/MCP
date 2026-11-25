from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import httpx
import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from tradedesk.analytics.rec_paper import entry_book, max_loss, open_paper, settle_paper, size_for_risk, summarize
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

F, T, IV = 100_000.0, 0.1, 0.5


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


ATM = F * (2 * _ncdf(IV * math.sqrt(T) / 2) - 1)  # analytic Black-76 ATM call (= put) price at r = 0
STRADDLE = [(F, True, 1.0, IV), (F, False, 1.0, IV)]


def test_atm_straddle_reference():
    book = entry_book(STRADDLE, F, F, T)
    assert book["premium"] == pytest.approx(2 * ATM, rel=1e-9)
    assert book["entry_fees"] == pytest.approx(2 * 0.0003 * F)  # fee cap (12.5 % of price) not binding


def test_max_loss_bounded_and_unbounded():
    book = entry_book(STRADDLE, F, F, T)
    loss, basis = max_loss(STRADDLE, book["premium"], book["entry_fees"], F)
    assert basis == "max_loss" and loss == pytest.approx(2 * ATM + 60.0)  # worst at the strike: premium + fees
    condor = [(110_000.0, True, -1.0, IV), (90_000.0, False, -1.0, IV), (120_000.0, True, 1.0, IV), (80_000.0, False, 1.0, IV)]
    cb = entry_book(condor, F, F, T)
    loss, basis = max_loss(condor, cb["premium"], cb["entry_fees"], F)
    # width - credit + entry fees + delivery fee of the in-the-money short call at the worst point (S = 120k)
    assert basis == "max_loss" and loss == pytest.approx(10_000.0 + cb["premium"] + cb["entry_fees"] + 0.00015 * 120_000.0)
    short = [(F, True, -1.0, IV), (F, False, -1.0, IV)]
    sb = entry_book(short, F, F, T)
    loss, basis = max_loss(short, sb["premium"], sb["entry_fees"], F)
    assert basis == "stress_up_30pct" and loss == pytest.approx(
        F + sb["premium"] + sb["entry_fees"]
    )  # the sold put at S = 0 dominates the +30 % call stress


def test_size_for_risk():
    assert size_for_risk(100.0, 291.0) == pytest.approx(0.3)  # 0.343 coins -> three 0.1 lots
    assert size_for_risk(100.0, 291.0, lot=0.01) == pytest.approx(0.34)
    assert size_for_risk(100.0, 291.0, lot=1.0) == 0.0
    assert size_for_risk(100.0, 0.0) == 0.0


def test_open_and_settle_long_straddle():
    rec = open_paper(STRADDLE, forward=F, spot=F, t_years=T, deposit_usd=10_000, risk_pct=1.0)
    assert rec["max_size_btc"] == 0.0 and rec["size_btc"] == 0.1 and rec["within_risk"] is False
    rec = open_paper(STRADDLE, forward=F, spot=F, t_years=T, deposit_usd=10_000_000, risk_pct=1.0)
    assert rec["within_risk"] and rec["size_btc"] == pytest.approx(math.floor(100_000 / (2 * ATM + 60.0) / 0.1 + 1e-9) * 0.1)
    legs = [[k, c, q, iv, p] for (k, c, q, iv), p in zip(STRADDLE, rec["leg_prices"], strict=True)]
    trade = {
        "legs": legs,
        "premium_per_btc": rec["premium"],
        "entry_fees_per_btc": rec["entry_fees"],
        "size_btc": rec["size_btc"],
        "risk_usd": rec["risk_usd"],
        "spot": F,
    }
    res = settle_paper(trade, 140_000.0)
    expect = 40_000.0 - 2 * ATM - 60.0 - 0.00015 * 140_000.0
    assert res["pnl_per_btc"] == pytest.approx(expect)
    assert res["pnl_usd"] == pytest.approx(expect * rec["size_btc"])
    assert res["pnl_pct_spot"] == pytest.approx(expect / F)
    assert res["r_worst_case"] == pytest.approx(res["pnl_usd"] / rec["risk_usd"])
    assert res["r_study"] == pytest.approx(expect / (2 * ATM))  # debit: risk is the premium
    flat = settle_paper(trade, F)  # no payoff: loses exactly the maximum loss, R = -1
    assert flat["r_worst_case"] == pytest.approx(-1.0)
    s = summarize([res, flat])
    assert s["n"] == 2 and s["win_share"] == 0.5 and s["mean_r_worst_case"] == pytest.approx((res["r_worst_case"] - 1.0) / 2)
    assert s["mean_r_study"] == pytest.approx((res["r_study"] + flat["r_study"]) / 2)
    assert summarize([]) == {"n": 0}


def test_r_study_credit_uses_method_q05():
    short = [(F, True, -1.0, IV), (F, False, -1.0, IV)]
    rec = open_paper(short, forward=F, spot=F, t_years=T, deposit_usd=10_000_000, risk_pct=1.0)
    legs = [[k, c, q, iv, p] for (k, c, q, iv), p in zip(short, rec["leg_prices"], strict=True)]
    trade = {
        "legs": legs,
        "premium_per_btc": rec["premium"],
        "entry_fees_per_btc": rec["entry_fees"],
        "size_btc": rec["size_btc"],
        "risk_usd": rec["risk_usd"],
        "spot": F,
    }
    assert rec["premium"] < 0
    assert settle_paper(trade, F)["r_study"] is None  # credit without q05
    res = settle_paper({**trade, "method_q05_pct": -0.10}, F)
    assert res["r_study"] == pytest.approx(res["pnl_per_btc"] / 10_000.0)


# ----------------------------------------------------------------------------- paper journal vs the simulation

# Booked fields of the live chain trade id 1 (2026-10-20, long_strangle10, rec_paper on the server, 2026-11-25). Its q05
# (-1.439 % of spot) is UNDER its worst reachable P&L, -max_loss / spot = -(premium + entry fees) / spot = -1.379 %.
SPOT1, FWD1, MEAN1, Q05_1, PREM1, MAXLOSS1 = 82535.87, 83021.21, 0.0603, -0.01431, 1088.71, 1138.23
# a ~14-day ATM straddle on the same entry: q05 -5.5 % of F = -5.53 % of spot, above its floor -(5000 + 49.52) / spot = -6.12 %
MEAN_SD, Q05_SD, PREM_SD, MAXLOSS_SD = 0.04, -0.055, 5000.0, 5049.52
T_OPEN = 1_792_000_000  # 2026-10-14
# (straddle?, P&L as a fraction of spot); every value is reachable (not under the trade's floor)
PATTERN = [(False, -0.01379), (True, -0.058), (False, 0.004), (True, -0.03), (False, 0.09), (True, 0.02), (False, -0.008)]
# the four live method rows (rec_paper on the server, 2026-11-25): id, spot, forward, method_q05_pct, max_loss_per_btc
LIVE_METHOD_ROWS = [
    (1, 82535.87, 83021.21, -0.01431, 1138.23),
    (2, 81798.68, 81985.73, -0.02704, 2167.63),
    (4, 77597.48, 78219.59, -0.0184, 1392.12),
    (5, 65718.09, 66096.83, -0.04307, 2806.85),
]


def _sim_row(i: int, pnl: float | None, straddle: bool = False, **kw) -> dict:
    opened = T_OPEN + i * 40 * 86400  # one chain trade every 40 days, held 38 (strangle) or 14 (straddle): no overlap
    row = {
        "id": i,
        "source": "method",
        "trigger": "chain",
        "status": "settled",
        "structure": "long_straddle" if straddle else "long_strangle10",
        "spot": SPOT1,
        "forward": FWD1,
        "method_mean_pct": MEAN_SD if straddle else MEAN1,
        "method_q05_pct": Q05_SD if straddle else Q05_1,
        "max_loss_per_btc": MAXLOSS_SD if straddle else MAXLOSS1,
        "risk_basis": "max_loss",
        "pnl_pct_spot": pnl,
        "r_study": None if pnl is None else pnl * SPOT1 / (PREM_SD if straddle else PREM1),
        "opened_ts": opened,
        "expiry_ts": opened + (14 if straddle else 38) * 86400,
    }
    row.update(kw)
    return row


def _sim_rows(n: int) -> list[dict]:
    return [_sim_row(i, PATTERN[i % 7][1], PATTERN[i % 7][0]) for i in range(n)]


def test_paper_vs_simulation_happy_path_from_min_n():
    import statistics

    from tradedesk.analytics.rec_paper import COMPARE_MIN_N, WALK_FORWARD_H1, paper_vs_simulation

    n = COMPARE_MIN_N
    rows = _sim_rows(n)
    c = paper_vs_simulation(rows)
    conv = FWD1 / SPOT1  # the booked mean / q05 are fractions of the FORWARD: put on the spot scale
    acts = [x["pnl_pct_spot"] for x in rows]
    exps = [x["method_mean_pct"] * conv for x in rows]
    diffs = [a - e for a, e in zip(acts, exps, strict=True)]
    sim, wf = c["vs_method_sim"], c["vs_walk_forward"]
    assert c["comparable"] == {"settled": n, "open": 0} and c["overlapping_pairs"] == 0 and c["ci_withheld"] is None
    assert sim["status"] == wf["status"] == "descriptive" and sim["n"] == wf["n"] == wf["n_r_study"] == n
    assert sim["mean_expected"] == pytest.approx(sum(exps) / n) and sim["mean_actual"] == pytest.approx(sum(acts) / n)
    assert sim["mean_diff"] == pytest.approx(sum(diffs) / n)
    sd = math.sqrt(sum((d - sum(diffs) / n) ** 2 for d in diffs) / (n - 1))
    half = 2.052 * sd / math.sqrt(n)  # Student t, df = 27
    assert sim["sd_diff"] == pytest.approx(sd)
    assert sim["ci95_mean_diff"] == pytest.approx([sim["mean_diff"] - half, sim["mean_diff"] + half])
    # q05: the 16 strangles' quantile is under their total loss (untestable); 4 of the 12 straddles fell below theirs
    assert sim["n_q05_at_floor"] == 16 and sim["n_with_q05"] == 12 and sim["n_below_q05"] == 4
    assert "share_below_q05" not in sim and sim["nominal_below_q05"] == 0.05  # 12 testable < 28: no share
    strangle, straddle = c["trades"][0], c["trades"][1]
    assert strangle["q05_at_floor"] is True and strangle["below_q05"] is None
    assert strangle["loss_floor_pct_spot"] == pytest.approx(-MAXLOSS1 / SPOT1) and strangle["q05_pct_spot"] == pytest.approx(Q05_1 * conv)
    assert straddle["q05_at_floor"] is False and straddle["below_q05"] is True
    assert wf["mean_pnl_pct_spot"] == pytest.approx(sum(acts) / n)
    assert wf["win_share"] == pytest.approx(sum(1 for a in acts if a > 0) / n)
    assert wf["mean_minus_reference"] == pytest.approx(sum(acts) / n + 0.0059)
    assert wf["median_r_study"] == pytest.approx(statistics.median(x["r_study"] for x in rows))
    # the reference is the report's H1 line, in the same unit (fraction of spot)
    assert wf["reference"]["mean_pnl_pct_spot"] == WALK_FORWARD_H1["mean_pnl_pct_spot"] == -0.0059
    assert wf["reference"]["ci95_mean_pnl_pct_spot"] == [-0.0243, 0.0151] and wf["reference"]["n"] == 27
    assert len(c["trades"]) == n and c["trades"][0]["expected_pct_spot"] == pytest.approx(MEAN1 * conv)
    # by holding period: straddles held 14 days, strangles 38; 12 and 16 < 28, so counts only
    hz = c["by_horizon"]
    assert hz["short_14d"]["n"] == 12 and hz["long_40d"]["n"] == 16 and hz["n_unclassified"] == 0
    assert hz["short_14d"]["status"] == hz["long_40d"]["status"] == "too_few" and "mean_pnl_pct_spot" not in hz["long_40d"]


def test_q05_under_the_total_loss_is_not_testable():
    """A bought strangle's booked q05 is the whole premium plus the simulation's fees (0.03 % of F per leg each way),
    under the trade's real worst P&L (premium + Deribit entry fees): the trade can never fall below it. Such rows are
    flagged and kept out of the share; the old rule counted them as 'not below', 0 of n by construction."""
    from tradedesk.analytics.rec_paper import paper_vs_simulation

    for i, spot, fwd, q05, ml in LIVE_METHOD_ROWS:  # all four live method rows sit there
        (t,) = paper_vs_simulation([_sim_row(i, -ml / spot, spot=spot, forward=fwd, method_q05_pct=q05, max_loss_per_btc=ml)])["trades"]
        assert t["q05_at_floor"] is True and t["below_q05"] is None, i
        assert t["q05_pct_spot"] < t["loss_floor_pct_spot"] - 0.0005, i  # ~0.06 pt of spot under the floor
    rows = [_sim_row(i, -MAXLOSS1 / SPOT1) for i in range(30)]  # each a total loss, the worst reachable P&L
    c = paper_vs_simulation(rows)
    sim = c["vs_method_sim"]
    assert sim["n_q05_at_floor"] == 30 and sim["n_with_q05"] == 0 and sim["n_below_q05"] == 0 and "share_below_q05" not in sim
    assert all(t["below_q05"] is None and t["q05_at_floor"] for t in c["trades"])
    # the same trades with a q05 above the floor are testable, and a total loss is below it
    up = paper_vs_simulation([{**x, "method_q05_pct": -0.012} for x in rows])["vs_method_sim"]
    assert up["n_q05_at_floor"] == 0 and up["n_with_q05"] == 30 and up["n_below_q05"] == 30 and up["share_below_q05"] == 1.0
    # an unbounded-loss row has no floor (testable); an unreadable floor is counted, not tested
    (stress,) = paper_vs_simulation([_sim_row(0, -0.01, risk_basis="stress_up_30pct")])["trades"]
    assert stress["q05_at_floor"] is False and stress["below_q05"] is False and stress["loss_floor_pct_spot"] is None
    bad = paper_vs_simulation([_sim_row(0, -0.01, max_loss_per_btc=None)])
    assert bad["missing_fields"]["max_loss"] == 1 and bad["trades"][0]["below_q05"] is None and bad["trades"][0]["q05_at_floor"] is None


def test_by_horizon_splits_at_the_method_expiry_bands():
    from tradedesk.analytics.rec_paper import LONG_MIN_DAYS, SHORT_MAX_DAYS, paper_vs_simulation
    from tradedesk.collector.assistant_paper import SHORT_DTE_BAND
    from tradedesk.tools.assistant_paper import DTE_BAND

    assert SHORT_MAX_DAYS == SHORT_DTE_BAND[1] and LONG_MIN_DAYS == DTE_BAND[0]  # the method's bands, no new threshold
    rows = _sim_rows(28)
    hz = paper_vs_simulation(rows, min_n=10)["by_horizon"]
    for key, straddle in (("short_14d", True), ("long_40d", False)):
        part = [x for x in rows if (x["structure"] == "long_straddle") == straddle]
        g = hz[key]
        assert g["status"] == "descriptive" and g["n"] == len(part), key
        assert g["mean_pnl_pct_spot"] == pytest.approx(sum(x["pnl_pct_spot"] for x in part) / len(part)), key
        assert g["mean_r_study"] == pytest.approx(sum(x["r_study"] for x in part) / len(part)), key
        assert g["ci95_mean_pnl_pct_spot"] is not None and g["mean_diff"] is not None, key
    edge = [_sim_row(i, 0.0) for i in range(4)]
    for x, days in zip(edge, (21, 28, 24, None), strict=True):
        x["expiry_ts"] = None if days is None else x["opened_ts"] + days * 86400
    hz = paper_vs_simulation(edge)["by_horizon"]
    assert hz["short_14d"]["n"] == 1 and hz["long_40d"]["n"] == 1 and hz["n_unclassified"] == 2


def test_paper_vs_simulation_bounds_and_bad_fields():
    from tradedesk.analytics.rec_paper import paper_vs_simulation

    rows = [
        _sim_row(0, 1e200),  # finite but beyond 10 x spot: not a P&L (its square would overflow the SD)
        _sim_row(1, 0.01, spot=0),
        _sim_row(2, 0.01, forward=-5.0),
        _sim_row(3, 0.01, method_mean_pct=True),  # bool is not a number
        _sim_row(4, 0.01, r_study=1e6),
        _sim_row(5, 0.01, expiry_ts=T_OPEN),  # expiry before entry
        _sim_row(6, 0.01, opened_ts=10**13),  # after year 9999
    ]
    c = paper_vs_simulation(rows)
    assert c["missing_fields"] == {
        "pnl_pct_spot": 1,
        "spot_forward": 2,
        "method_mean_pct": 1,
        "method_q05_pct": 0,
        "max_loss": 0,
        "r_study": 1,
        "holding_period": 2,
    }
    assert c["vs_walk_forward"]["n"] == 6 and c["by_horizon"]["n_unclassified"] == 2
    full = paper_vs_simulation(_sim_rows(28) + [_sim_row(40, 1e200), _sim_row(41, 0.01, r_study=1e300)])
    assert all(math.isfinite(v) for v in full["vs_walk_forward"]["ci95_mean_pnl_pct_spot"] + full["vs_walk_forward"]["ci95_mean_r_study"])


def test_paper_vs_simulation_empty_and_min_n_boundary():
    from tradedesk.analytics.rec_paper import COMPARE_MIN_N, paper_vs_simulation

    c = paper_vs_simulation([])
    assert c["comparable"] == {"settled": 0, "open": 0} and c["trades"] == []
    assert c["vs_method_sim"] == {
        "n": 0,
        "n_with_q05": 0,
        "n_below_q05": 0,
        "n_q05_at_floor": 0,
        "nominal_below_q05": 0.05,
        "status": "too_few",
    }
    assert c["vs_walk_forward"]["status"] == "too_few" and "mean_pnl_pct_spot" not in c["vs_walk_forward"]
    few = paper_vs_simulation(_sim_rows(COMPARE_MIN_N - 1))
    sim, wf = few["vs_method_sim"], few["vs_walk_forward"]
    # below min n: counts and per-trade raw numbers only
    assert sim["status"] == wf["status"] == "too_few" and sim["n"] == COMPARE_MIN_N - 1 and sim["n_below_q05"] == 4
    for k in ("mean_diff", "ci95_mean_diff", "share_below_q05", "mean_actual"):
        assert k not in sim, k
    for k in ("mean_pnl_pct_spot", "ci95_mean_pnl_pct_spot", "win_share", "mean_r_study", "median_r_study"):
        assert k not in wf, k
    assert len(few["trades"]) == COMPARE_MIN_N - 1 and few["trades"][0]["actual_minus_expected"] is not None
    assert paper_vs_simulation(_sim_rows(COMPARE_MIN_N))["vs_method_sim"]["status"] == "descriptive"


def test_paper_vs_simulation_never_mixes_triggered_or_manual_rows():
    from tradedesk.analytics.rec_paper import paper_vs_simulation

    base = _sim_rows(30)
    others = [
        _sim_row(100, 0.5, trigger="regime"),
        _sim_row(101, 0.4, trigger="event", status="open", pnl_pct_spot=None),
        _sim_row(102, 0.6, source="manual", trigger=None),
        _sim_row(103, None, status="open"),  # an open chain trade: counted as open, not compared
        _sim_row(104, 0.7, source=None),  # neither 'method' nor 'manual': counted under 'other', not lost
        _sim_row(105, None, source="mehtod", status="open"),
    ]
    a, b = paper_vs_simulation(base), paper_vs_simulation(base + others)
    assert b["excluded"] == {
        "method_triggered": {"settled": 1, "open": 1},
        "manual": {"settled": 1, "open": 0},
        "other": {"settled": 1, "open": 1},
    }
    assert b["comparable"] == {"settled": 30, "open": 1} and a["excluded"]["manual"] == {"settled": 0, "open": 0}
    assert a["vs_method_sim"] == b["vs_method_sim"] and a["vs_walk_forward"] == b["vs_walk_forward"]
    assert a["by_horizon"] == b["by_horizon"]
    # a NULL trigger on a method row is the chain (as summary_settled.method)
    c = paper_vs_simulation([_sim_row(0, 0.01, trigger=None)])
    assert c["comparable"]["settled"] == 1


def test_paper_vs_simulation_counts_missing_fields_and_withholds_the_interval_on_overlap():
    from tradedesk.analytics.rec_paper import paper_vs_simulation

    rows = _sim_rows(30)
    rows[0] = {**rows[0], "method_mean_pct": None}
    rows[1] = {**rows[1], "pnl_pct_spot": "x"}  # not a number: out of every statistic, counted
    rows[2] = {**rows[2], "forward": None}
    rows[3] = {**rows[3], "method_q05_pct": float("nan"), "r_study": None}
    c = paper_vs_simulation(rows)
    assert c["missing_fields"] == {
        "pnl_pct_spot": 1,
        "spot_forward": 1,
        "method_mean_pct": 1,
        "method_q05_pct": 1,
        "max_loss": 0,
        "r_study": 1,
        "holding_period": 0,
    }
    sim, wf = c["vs_method_sim"], c["vs_walk_forward"]
    assert c["comparable"]["settled"] == 30 and wf["n"] == 29 and wf["n_r_study"] == 28
    assert sim["n"] == 27 and sim["status"] == "too_few"  # rows 0 and 2 have no expectation on the spot scale
    # q05 on the spot scale for 27 rows (not 1: skipped, 2: no forward, 3: NaN q05): 11 straddles testable, 16 at the floor
    assert sim["n_with_q05"] == 11 and sim["n_q05_at_floor"] == 16
    assert wf["status"] == "descriptive" and wf["ci95_mean_pnl_pct_spot"] is not None
    # overlapping holding periods: means stay, the interval goes, with the reason
    ov = _sim_rows(30)
    ov[1] = {**ov[1], "opened_ts": ov[0]["opened_ts"] + 86400}
    c = paper_vs_simulation(ov)
    assert c["overlapping_pairs"] == 1 and "overlapping" in c["ci_withheld"]
    assert c["vs_method_sim"]["ci95_mean_diff"] is None and c["vs_method_sim"]["mean_diff"] is not None
    assert c["vs_walk_forward"]["ci95_mean_pnl_pct_spot"] is None and c["vs_walk_forward"]["ci95_mean_r_study"] is None
    unknown = _sim_rows(30)
    unknown[5] = {**unknown[5], "expiry_ts": None}
    c = paper_vs_simulation(unknown)
    assert c["missing_fields"]["holding_period"] == 1 and "unknown" in c["ci_withheld"]
    assert c["vs_walk_forward"]["ci95_mean_pnl_pct_spot"] is None


# ----------------------------------------------------------------------------- tool

DELIVERY = "2026-11-27"


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


def _rt(tmp_path, delivery_price: float | None = 60_000.0, status: int = 200) -> Runtime:
    state = State()
    state.deribit_status = status
    tr = make_transports(state)
    base = tr["deribit"].handler

    def deribit(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("get_delivery_prices") and status == 200:
            rows = [] if delivery_price is None else [{"date": DELIVERY, "delivery_price": delivery_price}]
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"data": rows, "records_total": len(rows)}})
        return base(req)

    tr["deribit"] = httpx.MockTransport(deribit)
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "rec.sqlite")),
        transports=tr,
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    return rt


async def test_open_settle_roundtrip(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="long_strangle25", deposit_usd=100_000)
        assert o["ok"] is True, o
        d = o["data"]
        assert d["id"] == 1 and d["label"] == "hypothesis" and d["status"] == "open"
        assert d["expiry"].startswith(DELIVERY) and len(d["legs"]) == 2
        assert d["premium_usd_per_btc"] > 0 and d["risk_basis"] == "max_loss" and d["within_risk"] is True
        assert d["size_btc"] > 0 and any("HYPOTHESIS" in w for w in o["warnings"])
        # not yet expired -> stays open
        s = await _call(srv, "assistant_paper_status")
        assert s["data"]["summary_settled"] == {"method": {"n": 0}, "method_triggered": {"n": 0}, "manual": {"n": 0}}
        assert len(s["data"]["open"]) == 1
        # after expiry: settled at the delivery price, own table only (the owner's journal stays empty)
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        s = await _call(srv, "assistant_paper_status")
        assert s["ok"] is True and s["data"]["open"] == []
        (t,) = s["data"]["settled"]
        assert t["delivery_price"] == 60_000.0 and t["pnl_usd"] is not None and t["r_worst_case"] is not None
        assert t["r_study"] is not None and t["source"] == "manual"  # a debit trade: risk is the premium
        assert s["data"]["summary_settled"]["manual"]["n"] == 1 and s["data"]["summary_settled"]["method"] == {"n": 0}
        legs = t["legs"]
        pay = sum(q * max(k - 60_000.0, 0.0) for k, q in ((x["strike"], x["qty"]) for x in legs if x["type"] == "put"))
        assert t["pnl_usd"] == pytest.approx(
            (pay - d["premium_usd_per_btc"] - d["entry_fees_usd_per_btc"]) * d["size_btc"]
            - min(0.00015 * 60_000, 0.125 * pay) * d["size_btc"],
            abs=0.5,
        )
        await rt.store.init()
        assert await rt.store.table_count("journal") == 0
        again = await _call(srv, "assistant_paper_status")
        assert again["data"]["summary_settled"]["manual"]["n"] == 1
    finally:
        await rt.aclose()


async def test_sources_summarised_separately_and_validation(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        # the tool only books manual rows: 'method' is the collector job's tag
        async with Client(srv) as c:
            try:
                await c.call_tool("assistant_paper_open", {"structure": "long_strangle25", "source": "method"})
            except Exception:  # noqa: BLE001 - a validation error may surface as an exception or an error result
                pass
        await rt.store.init()
        assert await rt.store.rec_paper_list() == []  # whichever way it was refused, no row was written
        for tr in ("chain", "regime"):  # two method rows written the way the job writes them
            m = await _call(srv, "assistant_paper_open", structure="long_strangle25", deposit_usd=1e6, regime="calm", note="x")
            assert m["ok"] and m["data"]["source"] == "manual" and m["data"]["regime"] == "calm"
            await rt.store.init()  # the server lifespan closes the store after each call
            await rt.store.conn.execute(
                "UPDATE rec_paper SET source = 'method', trigger = ?, method_q05_pct = -0.05 WHERE id = ?", (tr, m["data"]["id"])
            )
            await rt.store.conn.commit()
        assert (await _call(srv, "assistant_paper_open", structure="long_straddle", deposit_usd=1e6))["ok"]
        bad = await _call(srv, "assistant_paper_open", structure="long_straddle", size_btc=0.15)
        assert bad["ok"] is False and bad["missing"][0]["field"] == "size_btc"
        eth = await _call(srv, "assistant_paper_open", structure="long_straddle", size_btc=0.1, currency="ETH")
        assert eth["ok"] is False and eth["missing"][0]["field"] == "size_btc"  # ETH lot is 1
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        full = (await _call(srv, "assistant_paper_status"))["data"]
        s = full["summary_settled"]
        # the chain trade (H1-comparable) and the regime-triggered one are summarised apart
        assert s["method"]["n"] == 1 and s["method_triggered"]["n"] == 1 and s["manual"]["n"] == 1
        # the comparison with the simulation: the chain trade only, too few for anything but raw numbers
        v = full["vs_simulation"]
        assert v["comparable"] == {"settled": 1, "open": 0} and v["min_n"] == 28
        assert v["excluded"] == {
            "method_triggered": {"settled": 1, "open": 0},
            "manual": {"settled": 1, "open": 0},
            "other": {"settled": 0, "open": 0},
        }
        assert v["vs_method_sim"]["status"] == "too_few" and v["vs_method_sim"]["n"] == 0  # no booked mean on this row
        assert v["missing_fields"]["method_mean_pct"] == 1
        assert v["vs_walk_forward"]["status"] == "too_few" and v["vs_walk_forward"]["reference"]["mean_pnl_pct_spot"] == -0.0059
        (t,) = v["trades"]
        (row,) = [x for x in full["settled"] if x["id"] == t["id"]]
        assert row["source"] == "method" and row["trigger"] == "chain"
        # the booked q05 (-5 % of F) against this strangle's own worst loss (the stored max_loss_per_btc)
        floor = -row["max_loss_usd_per_btc"] / row["spot"]
        assert t["loss_floor_pct_spot"] == pytest.approx(floor, abs=1e-4) and t["q05_at_floor"] == (t["q05_pct_spot"] <= floor)
        assert v["vs_method_sim"]["n_with_q05"] + v["vs_method_sim"]["n_q05_at_floor"] == 1
        assert t["actual_pct_spot"] == pytest.approx(row["pnl_pct_spot"], abs=1e-5)
        assert t["opened"].startswith("2026-") and t["expiry"].startswith(DELIVERY) and t["expected_pct_spot"] is None
    finally:
        await rt.aclose()


async def test_small_deposit_flags_risk(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="short_straddle", expiry="30OCT26")
        assert o["ok"] is True
        assert o["data"]["within_risk"] is False and o["data"]["size_btc"] == 0.1
        assert o["data"]["risk_basis"] == "stress_up_30pct"
        assert any("budget" in w for w in o["warnings"]) and any("unbounded" in w for w in o["warnings"])
        dup = await _call(srv, "assistant_paper_open", structure="short_straddle", expiry="30OCT26")
        assert any("already exist" in w for w in dup["warnings"])
    finally:
        await rt.aclose()


async def test_delivery_not_published_stays_open(tmp_path):
    rt = _rt(tmp_path, delivery_price=None)
    srv, _ = build_server(rt)
    try:
        await _call(srv, "assistant_paper_open", structure="long_straddle", deposit_usd=1e6)
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        s = await _call(srv, "assistant_paper_status")
        assert len(s["data"]["open"]) == 1 and any(m["field"] == "rec_paper:1" for m in s["missing"])
    finally:
        await rt.aclose()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "abc", 10**400])
async def test_non_finite_delivery_price_keeps_the_trade_open(tmp_path, bad):
    # a NaN / inf delivery price used to settle the trade for good on a non-number, a 300+-digit one raised out of the tool
    rt = _rt(tmp_path, delivery_price=bad)
    srv, _ = build_server(rt)
    try:
        await _call(srv, "assistant_paper_open", structure="long_straddle", deposit_usd=1e6)
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        s = await _call(srv, "assistant_paper_status", include_settled=True)
        assert len(s["data"]["open"]) == 1 and s["data"]["settled"] == []
        assert any(m["field"] == "delivery_prices:BTC" and m["reason"] == "parse_error" for m in s["missing"])
    finally:
        await rt.aclose()


async def test_provider_failure_never_raises(tmp_path):
    rt = _rt(tmp_path, status=500)
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="long_straddle")
        assert o["ok"] is False and o["missing"]
        await rt.store.init()
        assert await rt.store.rec_paper_list() == []
        ok = _rt(tmp_path)  # same db path: book it with a healthy provider, then fail the settle
        s2, _ = build_server(ok)
        try:
            assert (await _call(s2, "assistant_paper_open", structure="long_straddle", deposit_usd=1e6))["ok"]
        finally:
            await ok.aclose()
        bad = _rt(tmp_path, status=500)
        s3, _ = build_server(bad)
        try:
            bad.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
            st = await _call(s3, "assistant_paper_status")
            assert len(st["data"]["open"]) == 1 and st["missing"]
            # the comparison still comes back on what the store holds (a manual row: nothing comparable)
            v = st["data"]["vs_simulation"]
            assert v["comparable"] == {"settled": 0, "open": 0} and v["excluded"]["manual"] == {"settled": 0, "open": 1}
            assert v["vs_method_sim"]["status"] == "too_few" and v["trades"] == []
        finally:
            await bad.aclose()
    finally:
        await rt.aclose()


async def test_no_store_configured():
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
        transports=make_transports(State()),
        exchange_factory=FakeEx,
    )
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="long_straddle")
        s = await _call(srv, "assistant_paper_status")
        assert o["ok"] is False and s["ok"] is False
    finally:
        await rt.aclose()
