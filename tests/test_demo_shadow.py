"""analytics/demo_shadow.py and its part of scripts/demo_vs_backtest.py: selected but not executed demo signals (missed,
skipped, refused by the guard) against their shadow plans, on samples of the live store (24.10) and analytic
references."""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import demo_vs_backtest as script  # noqa: E402

from tradedesk.analytics import demo_shadow as ds  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars  # noqa: E402
from tradedesk.analytics.demo_vs_backtest import MIN_TRADES, backtest_outcome  # noqa: E402
from tradedesk.analytics.smart_money import t975  # noqa: E402
from tradedesk.analytics.stop_management import MAKER, SLIPPAGE, TAKER, StopRule  # noqa: E402
from tradedesk.analytics.trade_plan import r_multiple  # noqa: E402
from tradedesk.core.envelope import Evidence, Fetched  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402

FIX = json.loads((Path(__file__).parent / "fixtures" / "demo_shadow_live_2026-10-24.json").read_text())
NOW = int(FIX["now_ms"])
H = 3_600_000


# ---------------------------------------------------------------- R units


def test_shadow_r_by_hand():
    ru = 5 + TAKER * (100 + 95)  # live risk unit: distance plus the taker fee on both prices
    assert ds.shadow_r("long", 100, 95, 110, "target") == pytest.approx((10 - MAKER * 100 - MAKER * 110) / ru)
    assert ds.shadow_r("long", 100, 95, 95, "stop") == pytest.approx((-5 - MAKER * 100 - (TAKER + SLIPPAGE) * 95) / ru)
    ru_s = 5 + TAKER * (100 + 105)
    assert ds.shadow_r("short", 100, 105, 90, "target") == pytest.approx((10 - MAKER * 100 - MAKER * 90) / ru_s)
    assert ds.shadow_r("short", 100, 105, 103, "expired") == pytest.approx((-3 - MAKER * 100 - (TAKER + SLIPPAGE) * 103) / ru_s)
    assert ds.shadow_r("long", 100, 95, 110, "not_filled") is None
    assert ds.shadow_r("long", 100, 101, 110, "target") is None  # the stop on the wrong side: no risk unit
    assert ds.shadow_r("flat", 100, 95, 110, "target") is None


def _path(rows: list[tuple[float, float, float, float]], t0: int = 0) -> Bars:
    return Bars.from_rows([[t0 + i * H, *r] for i, r in enumerate(rows)], H)


def test_shadow_r_is_the_backtest_r_without_break_even():
    """Same units as demo_vs_backtest.r_plain: rule A of stop_management on the same exit."""
    up = _path([(100, 104, 99, 103), (103, 111, 102, 110)])
    a = backtest_outcome("long", up, 0, 100, 95, 110, 10 * H, StopRule("A"), now_ms=10 * H)
    assert a is not None and a.exit_kind == "target"
    assert ds.shadow_r("long", 100, 95, a.exit, "target") == pytest.approx(a.r_net)
    up_short = _path([(100, 101, 97, 98), (98, 106, 97, 105)])
    s = backtest_outcome("short", up_short, 0, 100, 105, 90, 10 * H, StopRule("A"), now_ms=10 * H)
    assert s is not None and s.exit_kind == "stop"
    assert s.exit == -105  # a short runs on negated prices
    assert ds.shadow_r("short", 100, 105, -s.exit, "stop") == pytest.approx(s.r_net)


# ---------------------------------------------------------------- matching and grouping


def test_reason_keys_keep_the_threshold_and_mask_the_value():
    assert ds.reason_key("score 1 < 2") == ds.reason_key("score 0 < 2") == "score N < 2"
    assert ds.reason_key("5 trades at risk today = max") == "N trades at risk today = max"
    assert ds.reason_key("price ran 0.31R from the planned entry (> 0.25R)") == "price ran NR from the planned entry (> 0.25R)"
    assert ds.reason_key("already in TIA/USDT:USDT") == "already in <symbol>"
    assert ds.reason_key(None) == ds.reason_key("  ") == ds.NO_REASON
    assert ds.parse_link("td-S3-s-4h-BTC-1792598400") == ("S3", "short", "4h")
    assert ds.parse_link("l-1") is None and ds.parse_link(None) is None


def test_real_sample_skipped_signals_meet_their_shadow_plans():
    rows = ds.match_rows(FIX["trades"], FIX["audit"], FIX["plans"], now_ms=NOW)
    skipped = [x for x in rows if x.group == "skipped"]
    taken = [x for x in rows if x.group == "taken"]
    assert len(skipped) == 9 and len(taken) == 3 and not [x for x in rows if x.group == "missed"]
    assert [x.status for x in skipped].count("resolved") == 8 and [x.status for x in skipped].count("open") == 1
    assert not [x for x in rows if x.status == "no_plan"]  # every skipped signal of the sample has its plan
    zec = next(x for x in skipped if x.symbol.startswith("ZEC"))
    assert (zec.status, zec.r, zec.setup, zec.side, zec.timeframe) == ("open", None, "S16", "short", "4h")
    plans = {p["id"]: p for p in FIX["plans"]}
    for x in rows:
        if x.status != "resolved":
            continue
        p = plans[x.plan_id]
        # the stored plan R is trade_plan's own formula: the right row and fields were joined
        assert x.r_plan == pytest.approx(r_multiple(p["side"], p["entry"], p["stop"], p["fill"], p["exit"]))
        assert x.r == pytest.approx(ds.shadow_r(p["side"], p["fill"], p["stop"], p["exit"], p["outcome"]))
        assert x.side == p["side"] and x.symbol == p["symbol"]
    # the live risk unit is wider than the planned risk and the fees differ: the units are not the same
    btc = next(x for x in skipped if x.symbol.startswith("BTC"))
    assert btc.outcome == "target" and btc.r < btc.r_plan


def test_real_sample_summary_by_reason_says_too_few():
    s = ds.not_executed(FIX["trades"], FIX["audit"], FIX["plans"], now_ms=NOW)
    g = s["groups"]["skipped"]
    rs = [x["r"] for x in s["rows"] if x["group"] == "skipped" and x["r"] is not None]
    assert g["n"] == 9 and g["resolved"] == 8 and g["open"] == 1 and g["n_r"] == 8
    assert g["outcomes"] == {"target": 5, "stop": 3, "expired": 0, "not_filled": 0}
    m = statistics.fmean(rs)
    h = t975(len(rs) - 1) * statistics.stdev(rs) / len(rs) ** 0.5
    assert g["mean_r"] == pytest.approx(m) and g["ci95_r"] == pytest.approx([m - h, m + h])
    assert g["win_share"] == pytest.approx(5 / 8) and g["sum_r"] == pytest.approx(sum(rs))
    assert not g["enough"] and g["min_n"] == MIN_TRADES and g["conclusion"].startswith("замало для висновку")
    by = {x["reason"]: x for x in g["reasons"]}
    assert list(by) == ["score N < 2", "N trades at risk today = max"]  # the most frequent first
    assert by["score N < 2"]["n"] == 8 and by["score N < 2"]["n_r"] == 7 and by["score N < 2"]["example"].startswith("score ")
    assert by["N trades at risk today = max"]["n_r"] == 1 and by["N trades at risk today = max"]["mean_r"] < 0  # AVAX stop
    t = s["groups"]["taken"]
    assert t["n"] == 3 and t["n_r"] == 3 and t["reasons"] == [] and t["outcomes"]["target"] == 3
    assert s["groups"]["missed"]["n"] == 0 and s["groups"]["missed"]["mean_r"] is None
    assert len(s["rows"]) == 12 and json.loads(json.dumps(s)) == s  # JSON-safe


def _missed(link: str, note: str | None, plan_id: int | None, opened: int) -> dict[str, Any]:
    return {
        "link_id": link, "symbol": "DOT/USDT:USDT", "side": "long", "setup": "S14", "timeframe": "4h", "signal_ts": opened - 2 * H,
        "status": "missed", "note": note, "plan_id": plan_id, "opened_ts": opened, "closed_ts": opened + 600_000,
    }  # fmt: skip


def test_missed_entries_use_the_linked_plan_and_the_order_note():
    opened = NOW - 2 * 86_400_000
    plan = {
        "id": 1001, "created_ts": opened // 1000, "symbol": "DOT/USDT:USDT", "side": "long", "entry": 4.0, "stop": 3.8,
        "target": 4.5, "tags": json.dumps(["src:demo-shadow", "setup:S14", "tf:4h", "demo:taken"]), "status": "resolved",
        "outcome": "target", "fill": 4.0, "exit": 4.5, "r_result": 2.47,
    }  # fmt: skip
    trades = [
        _missed("td-S14-l-4h-DOT-1", "price ran 0.31R from the planned entry (> 0.25R)", 1001, opened),
        _missed("td-S14-l-4h-DOT-2", "price at the stop before the fill: the idea is invalidated", None, opened),
        {**_missed("td-S14-l-4h-DOT-3", None, 1001, NOW + 1), "status": "missed"},  # decided after now: ignored
        {**_missed("td-S14-l-4h-DOT-4", None, None, opened), "status": "rejected"},  # never sent: not a missed entry
    ]
    s = ds.not_executed(trades, [], [plan], now_ms=NOW)
    g = s["groups"]["missed"]
    assert g["n"] == 2 and g["resolved"] == 1 and g["no_plan"] == 1 and g["week"] == 2
    assert g["mean_r"] == pytest.approx(ds.shadow_r("long", 4.0, 3.8, 4.5, "target"))
    reasons = [x["reason"] for x in g["reasons"]]
    assert (
        "price ran NR from the planned entry (> 0.25R)" in reasons
        and "price at the stop before the fill: the idea is invalidated" in reasons
    )


REF = json.loads((Path(__file__).parent / "fixtures" / "demo_shadow_refused_live_2026-10-24.json").read_text())
REF_NOW = int(REF["now_ms"])


def test_planless_refusals_follow_the_guard_wording():
    # execution.guard.check wording; execution.demo_trader._shadow writes no plan for these
    assert ds.planless_refusal(["levels: stop and target must be on opposite sides of the entry"])
    assert ds.planless_refusal(["stop 12.0% from the entry > 10%", "reward/risk 1.10 after fees < 1.5"])
    assert ds.planless_refusal(["reward/risk 0.79 after fees < 1.5", "stop 30.3% from the entry > 10%"])
    assert not ds.planless_refusal(["reward/risk 1.38 after fees < 1.5"])
    assert not ds.planless_refusal(["already in TIA/USDT:USDT"])
    assert not ds.planless_refusal(["maintenance + loss at all stops 2,100 USD > 20% of equity (cross margin)"])
    assert not ds.planless_refusal(["bybit: stop loss price invalid"])  # a venue error from _enter: its plan is written
    assert not ds.planless_refusal([]) and not ds.planless_refusal([None, 3])


def test_real_sample_guard_refusals_meet_their_shadow_plans():
    """Every 'refused' row of the live store up to 24.10 10:00 UTC: 44 with a plan, 34 without one by design."""
    s = ds.not_executed([], REF["audit"], REF["plans"], now_ms=REF_NOW)
    g = s["groups"]["refused"]
    assert len(REF["audit"]) == 78 and g["n"] == 44 and g["excluded"] == {"n": 34, "week": 34}
    assert g["no_plan"] == 0 and g["resolved"] == 27 and g["open"] == 17 and g["n_r"] == 27  # nothing lost
    assert g["outcomes"] == {"target": 11, "stop": 16, "expired": 0, "not_filled": 0}
    assert not g["enough"] and g["conclusion"].startswith("замало для висновку: тіньових планів з R 27")
    assert s["groups"]["skipped"]["n"] == 0  # the plans are tagged demo:skipped, the group comes from the audit kind
    plans = {p["id"]: p for p in REF["plans"]}
    rows = [x for x in s["rows"] if x["group"] == "refused"]
    for x in rows:
        p = plans[x["plan_id"]]
        assert (x["symbol"], x["side"], x["decided_ts"] // 1000) == (p["symbol"], p["side"], p["created_ts"])
        if x["status"] == "resolved":
            assert x["r"] == pytest.approx(ds.shadow_r(p["side"], p["fill"], p["stop"], p["exit"], p["outcome"]))
    assert len({x["plan_id"] for x in rows}) == 44  # one plan per refusal
    by = {x["reason"]: x for x in g["reasons"]}
    assert list(by) == ["reward/risk N after fees < 1.5", "already in <symbol>"]  # the threshold kept, the value masked
    rr = by["reward/risk N after fees < 1.5"]
    rs = [x["r"] for x in rows if x["reason"] == rr["reason"] and x["r"] is not None]
    assert rr["n"] == 36 and rr["n_r"] == 20 and rr["outcomes"]["target"] == 11 and rr["outcomes"]["stop"] == 9
    assert rr["mean_r"] == pytest.approx(statistics.fmean(rs)) and rr["sum_r"] == pytest.approx(sum(rs)) and not rr["enough"]
    assert by["already in <symbol>"]["n_r"] == 7 and by["already in <symbol>"]["outcomes"]["stop"] == 7
    assert json.loads(json.dumps(s)) == s


def test_a_planless_rule_with_a_plan_found_is_kept():
    """The exclusion is by design only: if a plan does exist for such a refusal, it is scored; a refusal of another rule
    without a plan stays visible as 'no_plan'."""
    a = next(x for x in REF["audit"] if "stop " in x["payload"])
    other = next(x for x in REF["audit"] if "already in" in x["payload"])
    plan = {
        "id": 9001, "created_ts": a["ts"] // 1000, "symbol": a["symbol"], "side": ds.parse_link(a["link_id"])[1],
        "tags": ["src:demo-shadow", f"setup:{a['setup']}", f"tf:{ds.parse_link(a['link_id'])[2]}", "demo:skipped"],
        "status": "open",
    }  # fmt: skip
    s = ds.not_executed([], [a, other], [plan], now_ms=REF_NOW)
    g = s["groups"]["refused"]
    assert g["n"] == 2 and g["open"] == 1 and g["no_plan"] == 1 and g["excluded"]["n"] == 0
    assert ds.planless_refusals([a, other], [], now_ms=REF_NOW) == [a["ts"]]
    assert ds.planless_refusals([a], [], now_ms=a["ts"] - 1) == []  # decided after now


def test_time_is_a_parameter():
    avax = next(a for a in FIX["audit"] if a["symbol"].startswith("AVAX"))
    rows = ds.match_rows(FIX["trades"], FIX["audit"], FIX["plans"], now_ms=avax["ts"] - 1)
    assert len([x for x in rows if x.group == "skipped"]) == 8  # AVAX was decided after this time
    s = ds.summary(rows, now_ms=avax["ts"] - 1)
    assert s["groups"]["skipped"]["week"] == 8 and s["now_ms"] == avax["ts"] - 1


def test_enough_plans_give_a_conclusion():
    rows = [
        ds.ShadowRow("skipped", "score N < 2", "score 1 < 2", None, "A", "S3", "4h", "long", NOW - i, None, "resolved", "target", 1.0, r)
        for i, r in enumerate([1.0, -1.0, 2.0] * 10)
    ]
    g = ds.summary(rows, now_ms=NOW)["groups"]["skipped"]
    assert g["enough"] and g["n_r"] == 30 and g["conclusion"].startswith("середній R тіньових планів +0.67")
    assert g["reasons"][0]["enough"]


def test_odd_rows_never_raise():
    audit = [
        {"ts": NOW - 1, "kind": "skipped", "link_id": "weird", "symbol": "X", "setup": "S3", "payload": "not json"},
        {"ts": "x", "kind": "skipped"},
        {"ts": NOW - 1, "kind": "refused", "link_id": "td-S3-l-4h-X-1", "payload": "{}"},  # no reason, no plan: a lost plan
        {"ts": NOW - 1, "kind": "refused", "payload": {"violations": "not a list"}},
        {"ts": NOW - 1, "kind": "dry_refused", "link_id": "td-S3-l-4h-X-2", "payload": "{}"},  # a dry run: not a decision
        {"ts": None, "kind": "refused"},
    ]
    plans = [{"id": 5, "tags": "not json"}, {"id": "x", "tags": ["src:demo-shadow"]}, {"tags": None}]
    trades = [{"status": "missed", "opened_ts": None, "signal_ts": None}, {"status": "closed", "opened_ts": NOW - 1, "plan_id": 99}]
    s = ds.not_executed(trades, audit, plans, now_ms=NOW)
    assert s["groups"]["skipped"]["n"] == 1 and s["groups"]["skipped"]["no_plan"] == 1
    assert s["groups"]["skipped"]["reasons"][0]["reason"] == ds.NO_REASON
    ref = s["groups"]["refused"]
    assert ref["n"] == 2 and ref["no_plan"] == 2 and ref["excluded"] == {"n": 0, "week": 0} and ref["mean_r"] is None
    assert [x["reason"] for x in ref["reasons"]] == [ds.NO_REASON]
    assert s["groups"]["taken"]["no_plan"] == 1
    bad = {"id": 7, "tags": ["src:demo-shadow"], "status": "resolved", "outcome": "stop", "side": "long", "fill": "x", "stop": 1, "exit": 1}
    r = ds.match_rows([{"status": "closed", "opened_ts": 1, "plan_id": 7, "side": "long"}], [], [bad], now_ms=NOW)
    assert r[0].status == "resolved" and r[0].r is None


# ---------------------------------------------------------------- script: read-only store, JSON file


def _store(tmp_path: Path, refused: bool = False) -> Path:
    """The 24.10 sample in a migrated store; with ``refused`` also every live 'refused' row and its plans."""
    from tradedesk.store.schema import MIGRATIONS

    p = tmp_path / "t.sqlite"
    con = sqlite3.connect(p)
    for v in sorted(MIGRATIONS):
        for sql in MIGRATIONS[v]:
            con.execute(sql)
    for a in FIX["audit"] + (REF["audit"] if refused else []):
        con.execute(
            "INSERT INTO exec_audit (ts, venue, kind, link_id, symbol, setup, payload) VALUES (?, 'bybit-demo', ?, ?, ?, ?, ?)",
            (a["ts"], a["kind"], a["link_id"], a["symbol"], a["setup"], a["payload"]),
        )
    # before both samples' now and the wall clock of any run (main() reads the real time)
    con.execute(
        "INSERT INTO exec_audit (ts, venue, kind, link_id, payload) VALUES (?, 'bybit-demo', 'refused', 'td-x', '{}')", (REF_NOW - 1,)
    )
    con.execute("INSERT INTO exec_audit (ts, venue, kind, link_id, payload) VALUES (?, 'bybit-demo', 'sent', 'td-y', '{}')", (REF_NOW - 1,))
    for p_ in FIX["plans"] + (REF["plans"] if refused else []):
        cols = ",".join(p_)
        con.execute(f"INSERT INTO trade_plan ({cols}) VALUES ({','.join('?' * len(p_))})", tuple(p_.values()))
    for t in FIX["trades"]:
        row = {**t, "venue": "bybit-demo", "qty": 1.0, "leverage": 10, "risk_usd": 100.0, "comment": "c"}
        con.execute(f"INSERT INTO demo_trade ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
    con.commit()
    con.close()
    return p


def test_script_reads_the_store_read_only(tmp_path):
    db = _store(tmp_path)
    before = db.stat().st_mtime_ns
    trades, audit, plans, err = script.load_not_executed(str(db))
    assert err is None and len(trades) == 3 and len(audit) == 10 and len(plans) == 12  # skipped + refused, not 'sent'
    assert db.stat().st_mtime_ns == before
    s = ds.not_executed(trades, audit, plans, now_ms=NOW)
    assert s["groups"]["skipped"]["n_r"] == 8 and s["groups"]["taken"]["n_r"] == 3
    assert s["groups"]["refused"]["n"] == 1 and s["groups"]["refused"]["no_plan"] == 1  # the bare refusal: a lost plan
    text = script.render_not_executed(s)
    assert "Відібрані, але не виконані сигнали проти тіньових планів" in text and "не відібрано: 9" in text
    assert "score N < 2: 8, з R 7" in text and "замало для висновку" in text and "відмова guard: 1" in text
    assert "без тіньового плану за правилом" not in text  # none excluded here
    (tmp_path / "r").mkdir()
    trades, audit, plans, err = script.load_not_executed(str(_store(tmp_path / "r", refused=True)))
    assert err is None and len(audit) == 9 + 78 + 1 and len(plans) == 12 + 44
    s = ds.not_executed(trades, audit, plans, now_ms=NOW)
    text = script.render_not_executed(s)
    assert "відмова guard: 45" in text and "reward/risk N after fees < 1.5: 36, з R 20" in text
    assert "без тіньового плану за правилом (стоп далі від входу, ніж дозволяє guard, або помилка рівнів): 34" in text
    assert script.load_not_executed(str(tmp_path / "none.sqlite"))[3].startswith("no database")
    assert "Звірки немає: no database" in script.render_not_executed({}, "no database")


class _NoCandles:
    async def ohlcv(self, *a: Any, **k: Any) -> Any:
        return Fetched([], Evidence(provider="fixture", tool="ohlcv"))

    async def public_get(self, *a: Any, **k: Any) -> Any:
        return Fetched([], Evidence(provider="fixture", tool="public_get"))


class _Failing:
    async def public_get(self, *a: Any, **k: Any) -> Any:
        raise ProviderError("ccxt:bybit", "network", "down")

    async def ohlcv(self, *a: Any, **k: Any) -> Any:
        raise ProviderError("ccxt:bybit", "network", "down")


def _runtime(ex: Any) -> type:
    class _Rt:
        def __init__(self, *_: Any) -> None:
            self.exchanges = ex

        async def aclose(self) -> None:
            pass

    return _Rt


def test_script_json_holds_the_not_executed_block(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "Runtime", _runtime(_NoCandles()))
    db = _store(tmp_path)
    out = tmp_path / "sub" / "latest.json"
    assert script.main(["--db", str(db), "--json", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == script.JSON_SCHEMA == 2 and isinstance(doc["generated_ms"], int) and doc["venue"] == "bybit"
    assert doc["summary"]["n"] == 3 and len(doc["rows"]) == 3  # the three closed demo trades of the sample
    ne = doc["not_executed"]
    assert ne["error"] is None and ne["groups"]["skipped"]["n"] == 9 and len(ne["rows"]) == 13  # + the bare refusal
    assert ne["groups"]["refused"]["excluded"] == {"n": 0, "week": 0}
    assert not (out.parent / "latest.json.tmp").exists()  # replaced atomically


def test_script_json_holds_the_guard_refusals(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "Runtime", _runtime(_Failing()))
    out = tmp_path / "latest.json"
    assert script.main(["--db", str(_store(tmp_path, refused=True)), "--json", str(out)]) == 0
    ref = json.loads(out.read_text(encoding="utf-8"))["not_executed"]["groups"]["refused"]
    assert ref["n"] == 45 and ref["no_plan"] == 1 and ref["n_r"] == 27 and ref["excluded"]["n"] == 34  # the real clock
    assert [x["reason"] for x in ref["reasons"]][:2] == ["reward/risk N after fees < 1.5", "already in <symbol>"]


def test_script_json_survives_a_provider_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "Runtime", _runtime(_Failing()))
    out = tmp_path / "latest.json"
    assert script.main(["--db", str(_store(tmp_path)), "--json", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["summary"]["counts"]["no_data"] == 3 and doc["notes"]  # the failure is in the notes, not raised
    assert doc["not_executed"]["groups"]["skipped"]["n_r"] == 8  # the shadow block needs no network


def test_unit_writes_where_the_dashboard_reads():
    root = Path(__file__).resolve().parent.parent
    unit = (root / "deploy" / "user-units" / "tradedesk-demo-vs-backtest-daily.service").read_text(encoding="utf-8")
    timer = (root / "deploy" / "user-units" / "tradedesk-demo-vs-backtest-daily.timer").read_text(encoding="utf-8")
    assert "scripts/demo_vs_backtest.py --json /var/lib/tradedesk-mcp/demo-vs-backtest/latest.json" in unit
    assert "tg_send" not in unit and "OnCalendar=*-*-* 07:40:00 Europe/Kyiv" in timer
