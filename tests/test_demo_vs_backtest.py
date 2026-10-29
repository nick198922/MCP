"""analytics/demo_vs_backtest.py and scripts/demo_vs_backtest.py: closed demo trades replayed on candles.

The real sample (tests/fixtures/demo_vs_backtest_bybit_2026-10-23.json): two closed demo trades of the server DB (DOT
short S3 4h, target; FIL long S3 4h, stop) with the Bybit mainnet 4h window of the live scan and the 1h path, fetched
2026-10-23 through rt.exchanges.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import demo_vs_backtest as script  # noqa: E402

from tradedesk.analytics import demo_vs_backtest as dvb  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars  # noqa: E402
from tradedesk.analytics.stop_management import StopRule, risk_unit  # noqa: E402
from tradedesk.core.envelope import Builder, Evidence, Fetched  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.store.db import Store  # noqa: E402

H1, H4 = 3_600_000, 14_400_000
HOLD_4H = 7 * 86_400_000
FIX = json.loads((Path(__file__).parent / "fixtures" / "demo_vs_backtest_bybit_2026-10-23.json").read_text())
NOW = int(FIX["now_ms"])


def _sample(coin: str) -> tuple[dict[str, Any], Bars, Bars]:
    t = next(x for x in FIX["trades"] if coin in x["row"]["link_id"])
    return dict(t["row"]), Bars.from_rows(t["bars"], H4), Bars.from_rows(t["path"], H1)


def _replay(row: Any, bars: Bars, path: Bars, now: int = NOW) -> dvb.Replay:
    return dvb.replay(row, bars, path, now_ms=now, hold_ms=HOLD_4H)


# ---------------------------------------------------------------- real sample


def test_real_short_target_is_a_match():
    row, bars, path = _sample("DOT")
    x = _replay(row, bars, path)
    assert x.verdict == "match" and x.reasons == ()
    assert x.bt_exit == "target" and row["exit_reason"] == "target"
    assert x.stop_diff_r < 0.01 and x.target_diff_r < 0.01 and x.entry_diff_r < 0.1
    # the hour that reached half the way also came back to the break-even level: R is a band [Bx, B]
    assert x.r_bt == pytest.approx(1.90, abs=0.01) and x.r_bt_fast == pytest.approx(0.01, abs=0.01)
    # the demo R in the backtest's risk units: its P&L over the risk from the signal close
    ru = dvb._risk_unit(row["fill"], row["stop"]) / dvb._risk_unit(x.bt_entry, x.bt_stop)
    assert x.r_demo_bt == pytest.approx(row["r_multiple"] * ru) and x.r_diff == pytest.approx(x.r_demo_bt - x.r_bt)
    assert x.r_bt_fill == pytest.approx(row["r_multiple"], abs=0.02)  # replayed from the demo fill: the demo's target R
    assert x.r_plain == pytest.approx(x.r_bt, abs=1e-9)  # no break-even: the same target
    # the short filled above the close (better) and planned above it too: both edges are positive
    assert 0 < x.entry_edge_r < x.plan_edge_r < 0.1 and x.entry_diff_r == pytest.approx(x.entry_edge_r)


def test_real_long_stop_is_a_match():
    row, bars, path = _sample("FIL")
    x = _replay(row, bars, path)
    assert x.verdict == "match"
    assert x.bt_exit == "stop" and row["exit_reason"] == "stop"
    assert x.r_bt == pytest.approx(-1.0, abs=0.02) and x.r_bt_fast == pytest.approx(x.r_bt)
    assert x.bt_entry > x.bt_stop and x.bt_target > x.bt_entry


def test_real_other_bar_is_no_signal_other_side_is_a_mismatch():
    row, bars, path = _sample("DOT")
    assert _replay({**row, "signal_ts": row["signal_ts"] - H4}, bars, path).verdict == "no_signal"
    x = _replay({**row, "side": "long"}, bars, path)  # the detector has a short on that bar, not a long
    assert x.verdict == "mismatch" and x.reasons == ("side",) and "short" in x.note


def test_real_levels_or_r_differ_is_a_mismatch():
    row, bars, path = _sample("FIL")
    risk = abs(row["entry"] - row["stop"])
    x = _replay({**row, "stop": row["stop"] - 0.2 * risk}, bars, path)
    assert x.verdict == "mismatch" and "stop" in x.reasons
    x = _replay({**row, "fill": row["fill"] + 0.5 * risk}, bars, path)
    assert x.verdict == "mismatch" and x.reasons == ("entry",)
    x = _replay({**row, "r_multiple": 0.5}, bars, path)
    assert x.verdict == "mismatch" and x.reasons == ("r",) and x.r_diff == pytest.approx(x.r_demo_bt - x.r_bt)
    x = _replay({**row, "r_multiple": None}, bars, path)
    assert x.verdict == "mismatch" and x.reasons == ("no demo R",)


def test_r_is_compared_in_the_same_risk_units():
    # a long filled 0.2R worse: its stop loses one R of its own (larger) risk, 1.2R of the backtest's; still a match
    row, bars, path = _sample("FIL")
    risk = abs(row["entry"] - row["stop"])
    x = _replay({**row, "fill": _replay(row, bars, path).bt_entry + 0.2 * risk}, bars, path)
    assert x.verdict == "match" and x.reasons == ()
    assert x.entry_edge_r == pytest.approx(-0.2, abs=0.01) and x.r_demo == row["r_multiple"]
    assert x.r_demo_bt == pytest.approx(1.2 * row["r_multiple"], abs=0.02) and x.r_bt_fill == pytest.approx(-1.0, abs=0.02)
    # a short filled 0.2R better: its target R is ~2.6 of its own (smaller) risk, above the backtest band [0.01, 1.90]
    # from the close by more than 0.3, yet the backtest replayed from the fill gives the same R
    row, bars, path = _sample("DOT")
    x0 = _replay(row, bars, path)
    fill = x0.bt_entry + 0.2 * abs(x0.bt_entry - x0.bt_stop)
    ru = dvb._risk_unit(fill, row["stop"])
    r = (fill - row["target"] - 0.0002 * (fill + row["target"])) / ru  # maker entry and target, as the demo's P&L
    x = _replay({**row, "fill": fill, "r_multiple": r}, bars, path)
    assert r > max(x.r_bt, x.r_bt_fast) + 0.3  # the old comparison (demo R against the band from the close) said 'r'
    assert x.verdict == "match" and x.reasons == ()
    assert x.entry_edge_r == pytest.approx(0.2, abs=1e-9) and x.r_bt_fill == pytest.approx(r, abs=0.02)
    assert x.r_diff == pytest.approx(x.r_demo_bt - x.r_bt) and x.r_diff > 0  # the better entry is the whole gap


def test_cut_path_is_no_data_not_a_time_exit():
    row, bars, path = _sample("DOT")  # the target is hit in the 7th 1h bar
    close = row["signal_ts"] + H4
    head = path.slice(0, 3)
    assert _replay(row, bars, head, now=close + 3 * H1).verdict == "pending"  # the path reaches now
    # the same three bars seen after the horizon: the venue returned too few bars, not a time exit
    x = _replay(row, bars, head, now=close + HOLD_4H + H1)
    assert x.verdict == "no_data" and "cut" in x.note and x.r_bt is None and x.bt_exit is None
    # a 3-hour hole before the exit (bars 2 and 3 missing): the path is cut at the hole, the horizon is open
    keep = [i for i in range(len(path)) if i not in (2, 3)]
    holed = Bars(path.t[keep], path.o[keep], path.h[keep], path.low[keep], path.c[keep], H1)
    x = _replay(row, bars, holed)
    assert x.verdict == "no_data" and "cut" in x.note
    assert _replay(row, bars, holed, now=close + HOLD_4H + H1).verdict == "no_data"
    # a hole after the exit does not matter
    row, bars, path = _sample("FIL")  # stopped in the 2nd bar
    keep = [i for i in range(len(path)) if i not in (5, 6, 7)]
    holed = Bars(path.t[keep], path.o[keep], path.h[keep], path.low[keep], path.c[keep], H1)
    assert _replay(row, bars, holed).verdict == "match"


def test_later_bars_do_not_change_the_signal():
    row, bars, path = _sample("DOT")
    x0 = _replay(row, bars, path)
    s = row["signal_ts"]
    later = [[s + k * H4, 5.0, 9.0, 0.5, 0.6 + k] for k in (1, 2)]  # wild bars after the signal, closed by NOW
    more = Bars.from_rows(_bar_rows(row) + later, H4)
    assert int(more.t[-1]) + H4 <= NOW
    win0, win = dvb.setup_window(bars, s, NOW), dvb.setup_window(more, s, NOW)
    assert win0 is not None and win is not None and np.array_equal(win.t, win0.t) and np.array_equal(win.c, win0.c)
    x = _replay(row, more, path)
    assert (x.verdict, x.bt_entry, x.bt_stop, x.bt_target, x.r_bt) == (x0.verdict, x0.bt_entry, x0.bt_stop, x0.bt_target, x0.r_bt)


def _bar_rows(row: dict[str, Any]) -> list[list[float]]:
    return [list(b) for b in next(t["bars"] for t in FIX["trades"] if t["row"]["link_id"] == row["link_id"])]


def test_constants_follow_the_live_code():
    from tradedesk.analytics.stop_management import TAKER
    from tradedesk.execution import guard, orders
    from tradedesk.execution.bybit_demo import BybitDemo

    assert dvb.BE_FRAC == guard.BE_PATH
    assert dvb.Tolerance().entry_r == orders.MAX_CHASE_R
    assert TAKER == guard.TAKER_FEE  # the risk unit of both R
    assert dvb.LIVE_WINDOW == inspect.signature(BybitDemo.ohlcv).parameters["limit"].default - 1


def test_real_r_inside_the_band_is_a_match():
    row, bars, path = _sample("DOT")
    x = _replay({**row, "r_multiple": 0.0}, bars, path)  # a break-even exit: the optimistic bound of the backtest
    assert x.verdict == "match"


def test_time_is_a_parameter_pending_and_no_data():
    row, bars, path = _sample("DOT")
    close = row["signal_ts"] + H4
    assert _replay(row, bars, path, now=close + H1).verdict == "pending"  # one hour of path: neither stop nor target
    assert _replay(row, bars, path, now=close - 1).verdict == "no_data"  # the signal bar has not closed yet
    assert _replay(row, bars.slice(0, len(bars) - 1), path).verdict == "no_data"
    assert _replay(row, bars, Bars.from_rows([], H1)).verdict == "no_data"
    assert _replay(row, Bars.from_rows(FIX["trades"][0]["bars"], H1), path).verdict == "no_data"  # wrong timeframe


def test_unsupported_setup():
    row, bars, path = _sample("DOT")
    x = _replay({**row, "setup": "OPT-straddle"}, bars, path)
    assert x.verdict == "unsupported" and "no candle detector" in x.note


def test_window_is_the_live_one():
    row, bars, _ = _sample("DOT")
    win = dvb.setup_window(bars, row["signal_ts"], NOW)
    assert win is not None and len(win) == dvb.LIVE_WINDOW and int(win.t[-1]) == row["signal_ts"]
    assert len(dvb.setup_window(bars, row["signal_ts"], NOW, window=100)) == 100


# ---------------------------------------------------------------- break-even rule in force at the open


SWITCH = dvb.BE_SWITCH_MS


def test_be_switch_is_the_commit_of_the_new_rule():
    from datetime import UTC, datetime

    assert datetime(2026, 10, 22, 7, 12, 39, tzinfo=UTC).timestamp() * 1000 == SWITCH  # commit 8d82847, 10:12 Kyiv
    assert dvb.BE_ROE == 0.30


def test_be_rules_follow_the_open_time():
    name, lo, hi = dvb.be_rules(SWITCH - 1, 20)  # opened a millisecond before the switch: +30 % ROE at 20x
    assert name == "roe" and not lo.be_same_bar and hi.be_same_bar
    assert lo.trigger(100.0, 95.0, 110.0) == pytest.approx(101.5) and hi.trigger(100.0, 95.0, 110.0) == pytest.approx(101.5)
    assert lo.trigger(-100.0, -105.0, -90.0) == pytest.approx(-98.5)  # a short on negated prices: 1.5 % down
    name, lo, hi = dvb.be_rules(SWITCH, 20)  # at the switch: half the way, whatever the leverage
    assert name == "path" and lo.trigger(100.0, 95.0, 110.0) == pytest.approx(105.0) and hi.be_same_bar
    assert dvb.be_rules(SWITCH - 1, None)[0] == "path" and dvb.be_rules(SWITCH - 1, 0)[0] == "path"  # no leverage
    assert dvb.be_rules(SWITCH - 1, 20, switch_ms=SWITCH - 2)[0] == "path"  # the switch is a parameter


def _before(row: dict[str, Any], lev: float | None, **kw: Any) -> dict[str, Any]:
    return {**row, "opened_ts": SWITCH - 3_600_000, "closed_ts": SWITCH - 60_000, "leverage": lev, **kw}


def test_trade_after_the_switch_uses_todays_rule():
    row, bars, path = _sample("FIL")  # opened 2026-10-22 12:04 UTC, after the switch
    assert row["opened_ts"] > SWITCH
    x = _replay({**row, "leverage": 5}, bars, path)
    assert x.be_rule == "path" and x.verdict == "match" and x.extra == {}
    assert x.r_bt_fill_now is None and x.r_bt_fill_now_fast is None  # nothing to compare: the rule in force is today's
    assert dvb.DemoTrade.from_row({**row, "leverage": 5}).leverage == 5.0 and dvb.DemoTrade.from_row(row).leverage is None


def test_trade_before_the_switch_is_replayed_under_plus_30_roe():
    row, bars, path = _sample("DOT")  # short, target hit after the hour that crossed half the way and came back
    after = _replay(row, bars, path)
    assert after.be_rule == "path" and after.r_bt_fast == pytest.approx(0.01, abs=0.01)  # break-even possible
    # at 5x the old trigger is a 6 % move, beyond the target (5.8 % away): break-even never fires, R is the target's
    x = _replay(_before(row, 5), bars, path)
    assert x.be_rule == "roe" and x.verdict == "match" and x.bt_exit == "target"
    assert x.r_bt == pytest.approx(after.r_bt) and x.r_bt_fast == pytest.approx(x.r_bt)
    assert x.r_bt_fill_now == pytest.approx(after.r_bt_fill) and x.r_bt_fill_now_fast == pytest.approx(after.r_bt_fill_fast)
    # so a break-even exit cannot come from the old rule at 5x: 'r', though today's band [0.01, 2.02] holds it
    assert _replay({**row, "r_multiple": 0.0}, bars, path).verdict == "match"
    y = _replay(_before(row, 5, r_multiple=0.0), bars, path)
    assert y.verdict == "mismatch" and y.reasons == ("r",)
    # at 20x the trigger is 1.5 %: the backtest moves the stop early and exits at break-even
    z = _replay(_before(row, 20, r_multiple=0.0), bars, path)
    assert z.be_rule == "roe" and z.verdict == "match" and z.bt_exit == "breakeven"
    assert z.r_bt == pytest.approx(0.0, abs=0.01) and z.r_bt_fast == pytest.approx(0.0, abs=0.01)


def test_divergence_from_the_rule_change_alone_is_its_own_reason():
    # FIL long, stopped out; synthetic 50x so the old trigger (0.6 % above the fill) is reached before the stop
    row, bars, path = _sample("FIL")
    x = _replay(_before(row, 50, r_multiple=0.0), bars, path)  # the demo left at break-even: the old rule says so
    assert x.be_rule == "roe" and x.r_bt_fill == pytest.approx(0.0, abs=0.01)
    assert x.r_bt_fill_now == pytest.approx(-1.0, abs=0.01)  # today's rule would have been stopped out
    assert x.verdict == "mismatch" and x.reasons == ("be_rule",)
    # the same trade opened at the switch: today's rule only, a plain R divergence
    y = _replay({**_before(row, 50, r_multiple=0.0), "opened_ts": SWITCH}, bars, path)
    assert y.be_rule == "path" and y.reasons == ("r",)
    # the demo R of the stop disagrees with the old rule: 'r', not the rule change
    assert _replay(_before(row, 50), bars, path).reasons == ("r",)
    s = dvb.summary([x, y, _replay(row, bars, path)])
    assert s["mismatch_reasons"] == {"be_rule": 1, "r": 1} and s["mismatch_rule_only"] == 1
    text = script.render([x, y], s, s, 0, NOW, "bybit", [])
    assert "лише зміна правила беззбитку 1" in text


def test_rule_edges_open_time_fallback_and_switch_inside():
    row, bars, path = _sample("DOT")
    # no leverage before the switch: today's rule, said so
    x = _replay(_before(row, None), bars, path)
    assert x.be_rule == "path" and "leverage unknown" in x.extra["be_rule_note"] and x.verdict == "match"
    # opened before, closed after the switch: the live monitor changed the rule mid-trade; flagged
    x = _replay({**row, "opened_ts": SWITCH - 1, "leverage": 10}, bars, path)
    assert x.be_rule == "roe" and x.extra == {"be_switch_inside": True}
    # no open time: the signal close decides (a signal bar closing before the switch)
    x = _replay({**row, "opened_ts": None, "leverage": 10}, bars, path, now=NOW)
    assert x.be_rule == "path"  # the DOT signal closed on 23.10
    x = dvb.replay({**row, "opened_ts": None, "leverage": 10}, bars, path, now_ms=NOW, hold_ms=HOLD_4H, be_switch_ms=NOW)
    assert x.be_rule == "roe"


# ---------------------------------------------------------------- analytic reference


def test_backtest_outcome_short_by_hand():
    t0 = 1_800_000_000_000 // H1 * H1
    rows = [[t0, 100, 100.5, 99, 99.5], [t0 + H1, 99.5, 99.8, 95.9, 96.2]]
    path = Bars.from_rows(rows, H1)
    o = dvb.backtest_outcome("short", path, t0, 100.0, 102.0, 96.0, HOLD_4H, StopRule("B", be_frac=0.5), t0 + 2 * H1)
    ru = 2.0 + 0.00055 * (100 + 102)  # live risk unit: distance plus the taker fee on both prices
    assert ru == pytest.approx(risk_unit(-100.0, -102.0))
    assert o is not None and o.exit_kind == "target" and o.exit == pytest.approx(-96.0)
    assert o.r_net == pytest.approx((4.0 - 0.0002 * (100 + 96)) / ru)  # maker entry and target
    # the same path seen before the second bar closed: the trade is still running
    o2 = dvb.backtest_outcome("short", path, t0, 100.0, 102.0, 96.0, HOLD_4H, StopRule("B", be_frac=0.5), t0 + H1)
    assert o2 is not None and o2.exit_kind == "data_end"


# ---------------------------------------------------------------- summary


def _r(verdict: str, setup: str = "S3", r_demo: float | None = 1.0, r_bt: float | None = 0.8, reasons=(), edge=None) -> dvb.Replay:
    diff = None if r_demo is None or r_bt is None else r_demo - r_bt
    return dvb.Replay(
        "x", "A/USDT:USDT", setup, "4h", "long", 0, 0, verdict, tuple(reasons),
        r_demo=r_demo, r_demo_bt=r_demo, r_bt=r_bt, r_diff=diff, entry_edge_r=edge,
    )  # fmt: skip


def test_summary_small_sample_says_too_few():
    rows = [_r("match", edge=0.1), _r("match", r_demo=-1.0, r_bt=-1.0, edge=-0.3)]
    rows += [_r("mismatch", reasons=("r",), r_demo=2.0, r_bt=1.0)]
    rows += [_r("no_signal", r_demo=-1.0, r_bt=None), _r("pending", r_bt=None), _r("unsupported", "OPT", r_bt=None)]
    s = dvb.summary(rows)
    assert s["n"] == 6 and s["decided"] == 4 and not s["enough"]
    assert s["counts"] == {"match": 2, "no_signal": 1, "mismatch": 1, "pending": 1, "no_data": 0, "unsupported": 1}
    assert s["per_setup"]["OPT"]["unsupported"] == 1 and s["per_setup"]["S3"]["match"] == 2
    assert s["mismatch_reasons"] == {"r": 1}
    assert s["n_r"] == 3 and s["mean_r_diff"] == pytest.approx((0.2 + 0.0 + 1.0) / 3)
    assert s["mean_r_demo_bt"] == pytest.approx(2.0 / 3) and s["match_share"] == pytest.approx(0.5)
    assert s["n_entry"] == 2 and s["mean_entry_edge_r"] == pytest.approx(-0.1)  # signed: the worse entry shows
    lo, hi = s["ci95_r_diff"]
    assert lo < s["mean_r_diff"] < hi
    assert s["conclusion"].startswith("замало для висновку") and "4" in s["conclusion"]


def test_summary_enough_trades_gives_shares():
    rows = [_r("match", r_demo=0.1 * (i % 3)) for i in range(27)] + [_r("no_signal", r_bt=None)] * 3
    s = dvb.summary(rows)
    assert s["enough"] and s["decided"] == 30
    assert "90%" in s["conclusion"] and "різниця R" in s["conclusion"]
    assert dvb.summary([])["conclusion"].startswith("замало")


# ---------------------------------------------------------------- script


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "t.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        for t in FIX["trades"]:
            r = t["row"]
            await st.demo_trade_insert({**r, "venue": "bybit-demo", "qty": 1.0, "leverage": 10, "comment": "c"})
        await st.demo_trade_insert(
            {**FIX["trades"][0]["row"], "link_id": "open-one", "status": "open", "venue": "v", "qty": 1, "leverage": 1, "comment": "c"}
        )
        await st.aclose()

    asyncio.run(go())
    return db


def test_script_replays_closed_rows_read_only(tmp_path):
    db = _db(tmp_path)
    rows, err = script.load_closed(db, 0, NOW + 1)
    assert err is None and {r["link_id"] for r in rows} == {t["row"]["link_id"] for t in FIX["trades"]}  # no open row
    assert script.load_closed(str(tmp_path / "none.sqlite"), 0, NOW)[1].startswith("no database")

    by = {(t["row"]["symbol"], tf): t[key] for t in FIX["trades"] for tf, key in (("4h", "bars"), ("1h", "path"))}

    async def fetch(symbol: str, tf: str, since: int, until: int) -> list[list[float]]:
        return [x for x in by[(symbol, tf)] if since <= x[0] <= until]

    out = asyncio.run(script.replay_all(rows, fetch, NOW))
    assert [x.verdict for x in out] == ["match", "match"]
    s = dvb.summary(out)
    text = script.render(out, s, s, 0, NOW, "bybit", ["примітка"])
    assert "Демо проти бектесту" in text and "замало для висновку" in text and "| DOT/USDT" in text and "Примітка: примітка" in text
    assert "- S3: збіг 2, немає сигналу 0, розбіжність 0, не вирішено 0." in text  # a line per setup
    assert "| +0.09 / +0.04 |" in text  # the signed entry edge (plan / fill) of the DOT short
    # the week block holds only the trades closed in it; the running total keeps the rest
    dot = next(x for x in out if "DOT" in x.link_id)
    text = script.render(out, s, dvb.summary([dot]), int(dot.closed_ts), NOW, "bybit", [])
    assert "| DOT/USDT" in text and "| FIL/USDT" not in text and "Від першої демо-угоди: закритих демо-угод 2" in text


class _FixtureExchanges:
    async def ohlcv(self, venue: str, symbol: str, tf: str, limit: int, since: int, until: int) -> Any:
        key = "bars" if tf == "4h" else "path"
        rows = next(t[key] for t in FIX["trades"] if t["row"]["symbol"] == symbol)
        return Fetched([x for x in rows if since <= x[0] <= until][:limit], Evidence(provider="fixture", tool="ohlcv"))


class _FixtureRuntime:
    def __init__(self, *_: Any) -> None:
        self.exchanges = _FixtureExchanges()

    async def aclose(self) -> None:
        pass


def test_script_running_total_keeps_trades_closed_before_the_week(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "Runtime", _FixtureRuntime)
    db = _db(tmp_path)
    rows, total, week, week_ms, notes = asyncio.run(script.run(db, script._since(None), 0.1, "bybit", NOW))
    assert notes == [] and week_ms == NOW - int(0.1 * script.DAY_MS)
    assert total["n"] == 2 and total["decided"] == 2 and week["n"] == 1  # FIL closed a day earlier: total only
    assert total["min_n"] == 30 and not total["enough"]  # the threshold is applied to the running total
    assert script._since("2026-10-20") == 1792454400000 and asyncio.run(script.run(db, NOW, 7, "bybit", NOW))[1]["n"] == 0


class _FailingExchanges:
    async def ohlcv(self, *a: Any, **k: Any) -> Any:
        raise ProviderError("ccxt:bybit", "network", "down")


class _Rt:
    exchanges = _FailingExchanges()


def test_script_provider_failure_goes_to_missing(tmp_path):
    rows, _ = script.load_closed(_db(tmp_path), 0, NOW + 1)
    b = Builder()
    out = asyncio.run(script.replay_all(rows, script.venue_fetch(_Rt(), b, "bybit"), NOW))  # type: ignore[arg-type]
    assert [x.verdict for x in out] == ["no_data", "no_data"]
    assert b.missing and b.missing[0].reason == "network"
    s = dvb.summary(out)
    text = script.render(out, s, s, 0, NOW, "bybit", [])
    assert "немає даних 2" in text


def test_replay_accepts_the_dataclass():
    row, bars, path = _sample("FIL")
    t = dvb.DemoTrade.from_row(row)
    assert _replay(replace(t), bars, path).verdict == "match"


# ---------------------------------------------------------------- turnover group (owner 29.10: $1-5M apart from >= $5M)

DAY = 86_400_000
D0 = 1_792_627_200_000  # 2026-10-22 00:00 UTC


def test_turnover_group_bands_and_unknown():
    g = dvb.turnover_group
    assert [g(5e6), g(80e6), g(4_999_999.99), g(1e6), g(999_999.0), g(0.0)] == ["ge5m", "ge5m", "1to5m", "1to5m", "lt1m", "lt1m"]
    assert {g(None), g(float("nan")), g(float("inf")), g(-1.0)} == {"unknown"}  # never silently put in a band
    assert dvb.TURNOVER_GROUPS == ("ge5m", "1to5m", "lt1m", "unknown")


def test_entry_day_is_the_utc_day_of_the_fill_else_of_the_signal_close():
    row, _, _ = _sample("FIL")  # filled 2026-10-22 12:04 UTC
    t = dvb.DemoTrade.from_row(row)
    assert dvb.entry_day_ms(t) == D0
    # no fill time: the close of a 4h bar opened 20:00 UTC is midnight, so the entry day is the next one
    assert dvb.entry_day_ms(replace(t, opened_ts=None, signal_ts=D0 - 4 * H1)) == D0
    assert dvb.entry_day_ms(replace(t, opened_ts=None, signal_ts=D0 - 5 * H1)) == D0 - DAY
    assert dvb.entry_day_ms(replace(t, opened_ts=None, timeframe="option")) is None


def test_bybit_linear_id():
    assert dvb.bybit_linear_id("DOT/USDT:USDT") == "DOTUSDT" and dvb.bybit_linear_id("1000PEPE/USDT:USDT") == "1000PEPEUSDT"
    assert {dvb.bybit_linear_id(s) for s in ("BTC/USDC:USDC", "BTC/USDT", "BTC-27DEC26-100000-C", "A B/USDT:USDT")} == {None}


def test_day_turnover_reads_index_6_of_a_closed_day_only():
    k = [[str(D0 + DAY), "1", "1", "1", "1", "9", "7000000"], [str(D0), "1.1", "1.2", "1.0", "1.1", "2000000", "2345678.5"]]
    assert dvb.day_turnover(k, D0, now_ms=D0 + DAY) == pytest.approx(2_345_678.5)  # newest first, as Bybit sends
    assert dvb.day_turnover(k, D0 + DAY, now_ms=D0 + DAY + 1) is None  # the day is still forming: a partial turnover understates
    assert dvb.day_turnover(k, D0 - DAY, now_ms=D0 + DAY) is None  # no candle
    assert dvb.day_turnover([[str(D0), "1", "1", "1", "1", "9"]], D0, now_ms=D0 + DAY) is None  # no turnover field
    assert dvb.day_turnover([[str(D0), "1", "1", "1", "1", "9", "x"], ["y"]], D0, now_ms=D0 + DAY) is None
    assert dvb.day_turnover([[str(D0), "1", "1", "1", "1", "9", "-5"]], D0, now_ms=D0 + DAY) is None


def test_summary_by_turnover_keeps_the_30_trade_rule_per_group():
    big = [replace(_r("match"), turnover_usd=8e6, turnover_group="ge5m") for _ in range(30)]
    thin = [replace(_r("match"), turnover_group="1to5m"), replace(_r("mismatch", reasons=("r",)), turnover_group="1to5m")]
    unknown = [_r("no_signal", r_bt=None)]  # the default group: no turnover set
    by = dvb.summary_by_turnover(big + thin + unknown)
    assert tuple(by) == dvb.TURNOVER_GROUPS
    assert by["ge5m"]["decided"] == 30 and by["ge5m"]["enough"] and "100%" in by["ge5m"]["conclusion"]
    assert by["1to5m"]["decided"] == 2 and not by["1to5m"]["enough"] and by["1to5m"]["conclusion"].startswith("замало для висновку")
    assert by["1to5m"]["match_share"] == pytest.approx(0.5) and by["1to5m"]["counts"]["mismatch"] == 1
    assert by["unknown"]["n"] == 1 and by["lt1m"]["n"] == 0 and by["lt1m"]["conclusion"].startswith("замало")
    assert sum(s["n"] for s in by.values()) == len(big + thin + unknown)  # every trade in exactly one group
    assert dvb.summary(big + thin + unknown)["enough"]  # the whole sample would pass; the thin group alone does not


def test_script_adds_turnover_one_fetch_per_symbol_and_pages():
    rows = [
        {**_sample("FIL")[0], "link_id": "a", "opened_ts": D0 + 5 * H1},
        {**_sample("FIL")[0], "link_id": "b", "opened_ts": D0 + DAY + H1},
        {**_sample("FIL")[0], "link_id": "c", "opened_ts": D0 + 1005 * DAY},  # a second page of 1000 days
        {**_sample("FIL")[0], "link_id": "d", "symbol": "FIL/USDC:USDC"},  # no Bybit USDT perpetual id
        {**_sample("FIL")[0], "link_id": "e", "opened_ts": D0 + 1006 * DAY},  # the day has not closed by now
    ]
    reps = [dvb.Replay(r["link_id"], r["symbol"], "S3", "4h", "long", 0, 0, "match") for r in rows]
    calls: list[tuple[str, int, int]] = []
    tv = {D0: "6000000", D0 + DAY: "1500000", D0 + 1005 * DAY: "400000.5"}

    async def fetch_day(sym: str, start: int, end: int) -> list[Any]:
        calls.append((sym, start, end))
        return [[str(d), "1", "1", "1", "1", "1", v] for d, v in tv.items() if start <= d <= end]

    now = D0 + 1006 * DAY + H1
    out = asyncio.run(script.add_turnover(rows, reps, fetch_day, now))
    assert [x.turnover_group for x in out] == ["ge5m", "1to5m", "lt1m", "unknown", "unknown"]
    assert out[0].turnover_usd == pytest.approx(6e6) and out[3].turnover_usd is None and out[0].verdict == "match"
    assert calls == [("FILUSDT", D0, D0 + 1000 * DAY - 1), ("FILUSDT", D0 + 1000 * DAY, D0 + 1006 * DAY - 1)]
    assert all(e - s < 1000 * DAY for _, s, e in calls)  # never more than one page of daily candles a request
    assert out[0].as_dict()["turnover_group"] == "ge5m"  # in the JSON rows


class _TurnoverExchanges(_FixtureExchanges):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def public_get(self, venue: str, endpoint: str, params: dict[str, Any]) -> Any:
        self.calls.append({"venue": venue, "endpoint": endpoint, **params})
        if self.fail:
            raise ProviderError("ccxt:bybit", "network", "down")
        rows = [[str(D0), "1", "1", "1", "1", "1", "3200000.5"]] if params["symbol"] == "FILUSDT" else []
        return Fetched({"retCode": 0, "result": {"list": rows}}, Evidence(provider="fixture", tool="kline"))


def test_script_run_with_turnover_splits_the_total(tmp_path, monkeypatch):
    ex = _TurnoverExchanges()

    class Rt(_FixtureRuntime):
        def __init__(self, *_: Any) -> None:
            self.exchanges = ex

    monkeypatch.setattr(script, "Runtime", Rt)
    db = _db(tmp_path)
    rows, total, week, week_ms, notes = asyncio.run(script.run(db, 0, 7, "bybit", NOW, turnover=True))
    assert notes == [] and {x.symbol: x.turnover_group for x in rows} == {"FIL/USDT:USDT": "1to5m", "DOT/USDT:USDT": "unknown"}
    # DOT entered on 23.10 and NOW is 23.10 16:00 UTC: its day has not closed, so it is not asked for
    assert [(c["endpoint"], c["symbol"], c["interval"], c["category"]) for c in ex.calls] == [
        ("publicGetV5MarketKline", "FILUSDT", "D", "linear")
    ]
    by = dvb.summary_by_turnover(rows)
    assert by["1to5m"]["n"] == 1 and by["unknown"]["n"] == 1 and by["ge5m"]["n"] == 0
    text = script.render(rows, total, week, week_ms, NOW, "bybit", notes, by)
    assert "## За оборотом монети в день входу" in text
    assert "- $1–5M: закритих демо-угод 1. Звірено 1: збіг 1" in text and "- оборот невідомий: закритих демо-угод 1." in text
    assert "- ≥ $5M: закритих демо-угод 0." in text and text.count("замало для висновку: звірено") == 5  # total + 4 groups
    assert "## За оборотом" not in script.render(rows, total, week, week_ms, NOW, "bybit", notes)  # the weekly path
    # without turnover (the weekly Telegram message) nothing is fetched
    ex.calls.clear()
    assert {x.turnover_group for x in asyncio.run(script.run(db, 0, 7, "bybit", NOW))[0]} == {"unknown"} and ex.calls == []


def test_script_turnover_provider_failure_goes_to_missing_and_unknown(tmp_path, monkeypatch):
    ex = _TurnoverExchanges(fail=True)

    class Rt(_FixtureRuntime):
        def __init__(self, *_: Any) -> None:
            self.exchanges = ex

    monkeypatch.setattr(script, "Runtime", Rt)
    rows, total, *_, notes = asyncio.run(script.run(_db(tmp_path), 0, 7, "bybit", NOW, turnover=True))
    assert [x.verdict for x in rows] == ["match", "match"]  # the replay itself is untouched
    assert {x.turnover_group for x in rows} == {"unknown"} and notes == ["kline:bybit:FILUSDT:D: network"]
