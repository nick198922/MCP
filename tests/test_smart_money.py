"""Smart-money persistence analytics (IDEA-20261015-smart-money-persistence)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tradedesk.analytics.smart_money import (
    classify_account,
    detect_delta_start,
    exposure,
    ic_null_sd,
    interp,
    mde,
    mean_t_ci,
    net_book,
    net_flow,
    parse_portfolio,
    period_metrics,
    periods_needed,
    position_changes,
    realized_pnl_windows,
    replay_positions,
    sd_ci95,
    step_at,
    step_change,
    t975,
    weekly_points,
    window_change,
    window_edges,
)
from tradedesk.providers.hyperliquid import parse_positions

FIX = Path(__file__).parent / "fixtures"


def test_exposure_and_classes_on_fixture() -> None:
    positions = parse_positions(json.loads((FIX / "hyperliquid_clearinghouse_state_2026-10-15.json").read_text()))
    gross, net = exposure(positions)
    assert gross == pytest.approx(5795088.49321 + 40151643.52584, rel=1e-9)
    assert net == pytest.approx(5795088.49321 - 40151643.52584, rel=1e-9)
    av = 34925807.51
    # share = net / gross = -0.748 (not <= -0.8), lev = 1.32, low turnover -> directional
    assert classify_account(av, 5 * av, positions) == "directional"
    assert classify_account(av, 200 * av, positions) == "hft_directional"  # |share| 0.75 > 0.25
    assert classify_account(av, 200 * av, None) == "mm"
    assert classify_account(av, 5 * av, None) == "unknown"
    assert classify_account(2e9, 0, positions) == "system"
    assert classify_account(0, 0, positions) == "unknown"


def test_classes_mm_hedger_idle() -> None:
    av = 1_000_000.0
    balanced = [{"side": "long", "value_usd": 600_000}, {"side": "short", "value_usd": 500_000}]
    assert classify_account(av, 150 * av, balanced) == "mm"
    short = [{"side": "short", "value_usd": 900_000}, {"side": "long", "value_usd": 50_000}]
    assert classify_account(av, 2 * av, short) == "hedger"  # share -0.895, lev 0.95
    assert classify_account(av, 20 * av, short) == "directional"  # turnover too high for a hedger
    assert classify_account(av, 2 * av, [{"side": "long", "value_usd": 50_000}]) == "idle"
    assert classify_account(av, 2 * av, []) == "idle"


def test_interp_and_window_change() -> None:
    h = [(0, 0.0), (10, 100.0), (20, 50.0)]
    assert interp(h, 5) == pytest.approx(50.0)
    assert interp(h, 10) == pytest.approx(100.0)
    assert interp(h, 15) == pytest.approx(75.0)
    assert interp(h, -1) is None and interp(h, 21) is None and interp([], 0) is None
    assert window_change(h, 5, 15) == pytest.approx(25.0)
    assert window_change(h, -5, 15) is None
    assert window_edges(100, 10, 3) == [70, 80, 90, 100]


def test_step_at_and_step_change() -> None:
    h = [(0, 0.0), (10, 100.0), (20, 50.0)]
    assert step_at(h, 0) == 0.0 and step_at(h, 9) == 0.0 and step_at(h, 10) == 100.0 and step_at(h, 19) == 100.0
    assert step_at(h, 20) == 50.0
    assert step_at(h, -1) is None and step_at(h, 21) is None and step_at([], 0) is None
    assert step_change(h, 5, 15) == pytest.approx(100.0)
    assert step_change(h, 15, 20) == pytest.approx(-50.0)
    assert step_change(h, -5, 15) is None


def test_signal_at_t_unchanged_by_a_point_after_t_inside_history() -> None:
    """Catches the interpolation leak: a sparse point *after* the formation edge t (not only after the end of the
    history) must not move the formation-window PnL."""
    h = [(0, 0.0), (10, 10.0), (20, 20.0), (30, 30.0)]
    h2 = [(0, 0.0), (10, 10.0), (20, 999.0), (30, 30.0)]  # changed point at 20 > t = 15
    t = 15
    assert step_change(h, 5, t) == step_change(h2, 5, t) == pytest.approx(10.0)
    assert step_at(h, t) == step_at(h2, t)
    # the linear-interpolation window does leak (documented look-ahead in window_change)
    assert window_change(h, 5, t) != window_change(h2, 5, t)
    # formation and evaluation share the edge point, so they never overlap
    assert step_change(h, 5, t) + step_change(h, t, 25) == pytest.approx(step_change(h, 5, 25))


def test_window_before_t_is_unchanged_by_later_data() -> None:
    h = [(0, 0.0), (10, 30.0), (20, 10.0)]
    edges = window_edges(20, 10, 2)
    before = [step_change(h, a, b) for a, b in zip(edges, edges[1:], strict=False)]
    later = h + [(30, 1e9), (40, -1e9)]
    after = [step_change(later, a, b) for a, b in zip(edges, edges[1:], strict=False)]
    assert before == after
    fills = [{"time": 5, "closedPnl": "10", "fee": "1"}, {"time": 15, "closedPnl": "-4", "fee": "0.5"}]
    w1 = realized_pnl_windows(fills, edges, 0)
    w2 = realized_pnl_windows(fills + [{"time": 25, "closedPnl": "999", "fee": "0"}], edges, 0)
    assert w1 == w2 == [pytest.approx(9.0), pytest.approx(-4.5)]


def test_realized_pnl_coverage_end() -> None:
    fills = [{"time": 5, "closedPnl": "10", "fee": "0"}, {"time": 12, "closedPnl": "3", "fee": "0"}]
    # a truncated history ends at the last loaded fill (12): later windows are unknown, not zero
    assert realized_pnl_windows(fills, [0, 10, 20, 30], 0, coverage_end=12) == [pytest.approx(10.0), None, None]
    assert realized_pnl_windows(fills, [0, 10, 20, 30], 0, coverage_end=None) == [
        pytest.approx(10.0),
        pytest.approx(3.0),
        pytest.approx(0.0),
    ]


def test_sd_ci95() -> None:
    lo, hi = sd_ci95(1.0, 12)  # df 11: chi2 0.975 = 21.920, 0.025 = 3.816
    assert lo == pytest.approx(math.sqrt(11 / 21.920), rel=0.01)
    assert hi == pytest.approx(math.sqrt(11 / 3.816), rel=0.02)
    assert all(math.isnan(x) for x in sd_ci95(1.0, 1))


def test_realized_pnl_coverage() -> None:
    fills = [{"time": 15, "closedPnl": "5", "fee": "1"}, {"bad": 1}]
    assert realized_pnl_windows(fills, [0, 10, 20], coverage_start=12) == [None, None]
    assert realized_pnl_windows(fills, [0, 10, 20], coverage_start=10) == [None, pytest.approx(4.0)]


def test_parse_portfolio() -> None:
    raw = [
        ["month", {"accountValueHistory": [[2, "10"], [1, "5"]], "pnlHistory": [[1, "0"], [2, "3.5"], ["x", "1"]], "vlm": "7"}],
        ["junk"],
    ]
    p = parse_portfolio(raw)
    assert p["month"]["value"] == [(1, 5.0), (2, 10.0)]
    assert p["month"]["pnl"] == [(1, 0.0), (2, 3.5)]
    assert p["month"]["vlm"] == 7.0
    assert parse_portfolio({"x": 1}) == {}


def test_parse_portfolio_on_live_sample() -> None:
    """tests/fixtures/hyperliquid_portfolio_2026-10-17.json: Hyperliquid info ``portfolio``, live probe 2026-10-17
    07:12 UTC (account = top by pnl_month on 16.10, a system account), windows week / month / allTime / perpAllTime."""
    p = parse_portfolio(json.loads((FIX / "hyperliquid_portfolio_2026-10-17.json").read_text()))
    assert set(p) == {"week", "month", "allTime", "perpAllTime"}
    at = p["allTime"]
    assert len(at["pnl"]) == len(at["value"]) == 78
    assert at["pnl"][0] == (1747263418766, 0.0)
    steps = sorted(b[0] - a[0] for a, b in zip(at["pnl"], at["pnl"][1:], strict=False))
    assert steps[len(steps) // 2] / 3.6e6 == pytest.approx(168.0, abs=1.0)  # ~weekly points: edges need step_at
    assert len(p["month"]["pnl"]) == 47 and len(p["week"]["pnl"]) == 67
    assert p["month"]["pnl"][-1][0] == p["allTime"]["pnl"][-1][0] == 1792221151272
    assert p["perpAllTime"]["pnl"][-1][1] == pytest.approx(-500.0)


def test_period_metrics_perfect_and_reverse() -> None:
    sig = [float(i) for i in range(100)]
    m = period_metrics(sig, [s / 1000 for s in sig])
    assert m["ic"] == pytest.approx(1.0)
    # top 20 by signal: 80..99 -> median 0.0895; rest 0..79 -> median 0.0395
    assert m["d"] == pytest.approx(0.0895 - 0.0395)
    r = period_metrics(sig, [-s for s in sig])
    assert r["ic"] == pytest.approx(-1.0) and r["d"] < 0
    assert period_metrics(sig[:5], sig[:5])["ic"] is None
    with pytest.raises(ValueError):
        period_metrics([1.0], [1.0, 2.0])


def test_mean_t_ci_and_power() -> None:
    s = mean_t_ci([1.0, 2.0, 3.0])
    assert s["mean"] == pytest.approx(2.0) and s["sd"] == pytest.approx(1.0)
    h = 4.303 / math.sqrt(3)
    assert s["lo"] == pytest.approx(2.0 - h) and s["hi"] == pytest.approx(2.0 + h)
    assert mean_t_ci([])["n"] == 0 and mean_t_ci([5.0])["lo"] is None
    assert t975(1) == pytest.approx(12.706) and t975(100) == 1.96
    assert mde(0.1, 4) == pytest.approx(2.8 * 0.1 / 2)
    assert periods_needed(0.1, 0.05) == math.ceil((2.8 * 2) ** 2)  # 31.36 -> 32
    assert ic_null_sd(101) == pytest.approx(0.1)


def _log() -> list[dict]:
    a, b = "0xa", "0xb"
    return [
        # schema < 12: full sets per change
        {"ts": 100, "account": a, "market": "BTC", "side": "long", "size": 1.0, "value_usd": 100.0},
        {"ts": 100, "account": a, "market": "ETH", "side": "short", "size": 10.0, "value_usd": 20.0},
        {"ts": 100, "account": b, "market": "", "side": "flat", "size": None, "value_usd": None},
        {"ts": 200, "account": a, "market": "BTC", "side": "long", "size": 2.0, "value_usd": 220.0},  # ETH closed silently
        # schema 12 from ts 300: baseline then changes
        {"ts": 300, "account": a, "market": "BTC", "side": "long", "size": 2.0, "value_usd": 230.0},
        {"ts": 300, "account": b, "market": "SOL", "side": "short", "size": 5.0, "value_usd": 50.0},
        {"ts": 400, "account": a, "market": "DOGE", "side": "long", "size": 100.0, "value_usd": 10.0},
        {"ts": 400, "account": b, "market": "SOL", "side": "closed", "size": 0.0, "value_usd": None},
    ]


def test_replay_positions_full_then_delta() -> None:
    log = _log()
    assert detect_delta_start([r["ts"] for r in log], [r["ts"] for r in log if r["side"] == "closed"]) == 300
    s = replay_positions(log, 300)
    assert set(s["0xa"]) == {"BTC", "DOGE"} and s["0xb"] == {}
    s200 = replay_positions(log, 300, until=200)
    assert set(s200["0xa"]) == {"BTC"} and s200["0xa"]["BTC"]["size"] == 2.0 and s200["0xb"] == {}
    s300 = replay_positions(log, 300, until=300)
    assert set(s300["0xb"]) == {"SOL"}
    assert detect_delta_start([1, 2], []) is None


def test_replay_before_t_unchanged_by_later_rows() -> None:
    log = _log()
    base = replay_positions(log, 300, until=300)
    more = log + [{"ts": 500, "account": "0xa", "market": "BTC", "side": "closed", "size": 0.0, "value_usd": None}]
    assert replay_positions(more, 300, until=300) == base


def test_net_flow_and_book() -> None:
    log = _log()
    s100, s400 = replay_positions(log, 300, until=100), replay_positions(log, 300, until=400)
    flow = net_flow(s100, s400, ["0xa", "0xb"])
    # BTC +1 coin at the later mark 115 USD; ETH short 10 closed -> +10 coins at mark 2; DOGE +100 at 0.1
    assert flow["BTC"] == pytest.approx(115.0)
    assert flow["ETH"] == pytest.approx(20.0)
    assert flow["DOGE"] == pytest.approx(10.0)
    assert "SOL" not in flow  # opened and closed in between
    # an account without a baseline in ``before`` contributes no flow (its whole book is not a change)
    assert net_flow({}, s400, ["0xa"]) == {}
    assert net_flow({"0xb": {}}, s400, ["0xa", "0xb"]) == {}
    book = net_book(s400, ["0xa", "0xb"])
    assert book["BTC"] == {"long": 230.0, "short": 0.0, "net": 230.0, "holders": 1}


def test_position_changes_and_weekly_points() -> None:
    log = _log()
    s100, s400 = replay_positions(log, 300, until=100), replay_positions(log, 300, until=400)
    ch = {(c["account"], c["market"]): c["kind"] for c in position_changes(s100, s400, ["0xa", "0xb", "0xc"])}
    assert ch == {("0xa", "BTC"): "increased", ("0xa", "DOGE"): "opened", ("0xa", "ETH"): "closed"}
    usd = {c["market"]: c["delta_usd"] for c in position_changes(s100, s400, ["0xa"])}
    assert usd == {"BTC": pytest.approx(115.0), "DOGE": pytest.approx(10.0), "ETH": pytest.approx(20.0)}
    flip = position_changes({"x": {"BTC": {"side": "long", "size": 1}}}, {"x": {"BTC": {"side": "short", "size": 1}}}, ["x"])
    assert flip[0]["kind"] == "flipped"
    days = {"2026-10-16": 5.0, "2026-10-09": -1.0, "2026-10-02": None, "2026-09-25": 3.0, "2026-10-15": 9.0}
    assert weekly_points(days, "2026-10-16") == [5.0, -1.0]
    assert weekly_points({}, "2026-10-16") == []
