"""Recount runner of the hl-whale-liquidation-magnet card (registration «Повторна лічба», rules rev. 4): period bounds,
poll markers, stage guards, descriptive helpers and the runner glue on a synthetic store. No network, no real data."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from tradedesk.analytics.hl_liq_magnet import (
    DAY,
    HOUR,
    RECOUNT_SETTLE_S,
    close_reopen_pairs,
    recount_completeness,
    recount_outcome,
    recount_period,
    recount_runs,
    recount_stage,
    snapshot_age_days,
)
from tradedesk.store.schema import MIGRATIONS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_hl_liq_magnet_step0 as r  # noqa: E402

H = HOUR
BIG = 40_000.0  # coins: x mark 100 = $4M >= $3M


def test_recount_period_is_42_days_end_excluded_and_runs_include_marker_only_runs():
    p = 1_800_000_000
    assert recount_period(p) == (p, p + 42 * DAY)
    end = p + 42 * DAY
    # a run with markers only (every request failed) is still a run; ts outside [start, end) are not
    assert recount_runs([p - H, p + H, end], [p, p + 2 * H, p + H, end], p, end) == [p, p + H, p + 2 * H]


def test_recount_stage_refuses_before_the_period_end_and_flags_late_stages():
    e = 1_800_000_000
    for stage in ("prefetch", "count"):
        assert recount_stage(stage, e - DAY, e)["allowed"] is False
        assert recount_stage(stage, e + RECOUNT_SETTLE_S - 1, e)["allowed"] is False
        ok = recount_stage(stage, e + RECOUNT_SETTLE_S, e)
        assert ok["allowed"] is True and ok["late"] is False and ok["opens_at"] == e + RECOUNT_SETTLE_S
    assert recount_stage("prefetch", e + 2 * DAY + 1, e)["late"] is True
    assert recount_stage("count", e + 2 * DAY + 1, e)["late"] is False
    assert recount_stage("count", e + 7 * DAY + 1, e)["late"] is True
    assert recount_stage("smoke", e - 30 * DAY, e)["allowed"] is True
    with pytest.raises(ValueError):
        recount_stage("peek", e + DAY, e)


def test_snapshot_age_days_uses_only_days_already_written():
    d0 = 1_800_000_000 // DAY * DAY
    days = [(d0, d0 + 600), (d0 + DAY, d0 + DAY + 900)]
    assert snapshot_age_days(days, [d0 + 300, d0 + 700, d0 + DAY + 100, d0 + DAY + 1000, d0 + 3 * DAY]) == [None, 0, 1, 0, 2]


def _p(ts, market, side, size, liq=103.0):
    return {"ts": ts, "account": "a", "market": market, "side": side, "size": size, "value_usd": None, "liq_px": liq}


def test_close_reopen_pairs_counts_a_close_undone_at_the_next_run():
    runs = [0, H, 2 * H, 3 * H, 4 * H]
    base = (0, [_p(0, "BTC", "long", 2.0)])
    closed = (H, [_p(H, "BTC", "closed", 0.0)])
    assert close_reopen_pairs([base, closed, (2 * H, [_p(2 * H, "BTC", "long", 2.00001)])], runs, 0, 0, 5 * H) == 1
    assert close_reopen_pairs([base, closed, (3 * H, [_p(3 * H, "BTC", "long", 2.0)])], runs, 0, 0, 5 * H) == 0  # not next
    assert close_reopen_pairs([base, closed, (2 * H, [_p(2 * H, "BTC", "long", 3.0)])], runs, 0, 0, 5 * H) == 0  # size
    assert close_reopen_pairs([base, closed, (2 * H, [_p(2 * H, "BTC", "short", 2.0)])], runs, 0, 0, 5 * H) == 0  # side
    assert close_reopen_pairs([base, closed, (2 * H, [_p(2 * H, "BTC", "long", 2.0)])], runs, 0, 2 * H, 5 * H) == 0  # before start


# --- synthetic store (real schema) -------------------------------------------------------------------------------


def _store(path, positions, polls, snapshot=()):
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        for stmt in MIGRATIONS[v]:
            con.execute(stmt)
    con.executemany(
        "INSERT INTO trader_positions (ts, venue, account, market, side, size, value_usd, liq_px) VALUES (?,?,?,?,?,?,?,?)",
        [(ts, "hyperliquid", acc, m, side, size, None, liq) for ts, acc, m, side, size, liq in positions],
    )
    con.executemany("INSERT INTO trader_polls (venue, ts, account, ok) VALUES ('hyperliquid',?,?,?)", polls)
    con.executemany(
        "INSERT INTO trader_snapshot (day, venue, account, ts, account_value, pnl_month, pnl_all) VALUES (?,?,?,?,?,?,?)",
        snapshot,
    )
    con.commit()
    con.close()


P = 1_800_000_000 // DAY * DAY + 15 * 60  # first run with a marker
T0, T1 = P - 3 * H, P - 2 * H  # history runs before it (T1 has a close: the v12 cutover is T0)
POSITIONS = [
    (T0, "a", "BTC", "short", BIG, 103.0),
    (T0, "a", "ETH", "long", 1.0, None),
    (T1, "a", "ETH", "closed", 0.0, None),
    (T0, "d", "SOL", "long", 1.0, 90.0),  # only before the period, no marker: never observed in it
    (P + H, "a", "BTC", "short", BIG, 103.0),  # rewrite row at an ok marker: fresh
    (P + 3 * H, "a", "BTC", "short", BIG, 103.0),  # row without a marker
    (P + 42 * DAY, "a", "BTC", "short", BIG, 103.0),  # at the period end: excluded
]
POLLS = [
    (P, "a", 1),
    (P, "b", 1),  # b: markers, no rows ever (an ok poll of a flat book)
    (P, "c", 0),  # c: markers, never ok, no rows
    (P + H, "a", 1),
    (P + H, "b", 1),
    (P + H, "c", 0),
    (P + 2 * H, "a", 0),  # a run with markers only, all not ok
    (P + 2 * H, "b", 0),
    (P + 2 * H, "c", 0),
    (P + 3 * H, "b", 1),
    (P + 42 * DAY, "a", 1),  # at the period end: excluded
]


def test_load_recount_store_reads_markers_runs_and_accounts_without_rows(tmp_path):
    db = tmp_path / "s.sqlite"
    _store(db, POSITIONS, POLLS)
    assert r.load_polls_from(str(db)) == P
    st = r.load_recount_store(str(db), *recount_period(P))
    assert st["runs"] == [P, P + H, P + 2 * H, P + 3 * H]
    assert st["accounts"] == ["a", "b", "c", "d"] and st["accounts_marked_no_rows"] == 2
    assert st["polls"]["c"] == {P: False, P + H: False, P + 2 * H: False}
    assert st["polls"]["a"] == {P: True, P + H: True, P + 2 * H: False} and P + 42 * DAY not in st["polls"]["a"]
    assert st["n_markers"] == 10 and st["n_rows_period"] == 2
    assert r.detect_delta_start(st["log_runs"], st["closed"]) == T0  # cutover from the whole log
    empty = tmp_path / "e.sqlite"
    _store(empty, POSITIONS[:3], [])
    assert r.load_polls_from(str(empty)) is None


def test_recount_glue_observes_by_markers_and_carries_the_replayed_state(tmp_path):
    db = tmp_path / "s.sqlite"
    _store(db, POSITIONS, POLLS)
    start, end = recount_period(P)
    st = r.load_recount_store(str(db), start, end)
    ds = r.detect_delta_start(st["log_runs"], st["closed"])
    episodes, counters = r.new_tallies()
    calls: list = []
    orig = r.account_observations

    def spy(groups, runs, **kw):
        out = orig(groups, runs, **kw)
        calls.append(out[0])
        return out

    r.account_observations = spy
    try:
        for acc in st["accounts"]:
            r.add_account(
                episodes, counters, acc, r.account_groups(str(db), acc, end), st["runs"], delta_start=ds,
                watched=lambda x: True,  # ignored from polls_from on: the reconstructed list is not used
                top_at=lambda x: frozenset({"BTC", "ETH"}), mark=lambda c, x: 100.0,
                polls=st["polls"].get(acc, {}), polls_from=start,
            )  # fmt: skip
    finally:
        r.account_observations = orig
    # a: P ok, P+H ok, P+2H not ok (unpolled), P+3H rows without a marker; b: 3 ok + 1 not ok; c: 3 not ok; d: none
    assert counters["observed_runs"] == 11 and counters["unpolled_runs"] == 5 and counters["rows_unmarked"] == 1
    a = calls[0]  # accounts are sorted: a, b, c, d
    btc = a["fresh_1_4"]["BTC"]
    assert [o.ts for o in btc] == st["runs"]
    # P: the state replayed from the log (BTC short since T0), liq_px older than the run -> unknown, not empty
    assert [o.qualifies for o in btc] == [None, True, None, True]
    assert [o.qualifies for o in a["any_1_4"]["BTC"]] == [True, True, None, True]
    assert [o.qualifies for o in a["fresh_1_4"]["ETH"]] == [False, False, None, False]  # closed before P
    assert calls[1]["fresh_1_4"] == {} and calls[3]["fresh_1_4"] == {}  # b holds nothing; d is never observed
    assert btc[0].data_start and btc[0].first_obs  # 'start' is counted from polls_from
    assert counters["in_scope"] == 3 and counters["in_scope_fresh"] == 2
    comp = recount_completeness(counters, 3 * H, 42 * DAY)
    assert {"polled", "known", "exposure", "unmarked"} <= set(comp["failed"])
    assert recount_outcome({"passed": True}, comp) == "incomplete"


# --- the runner's stage guard: refusal before the period end, no network -------------------------------------------


def _ns(db, tmp_path, **kw):
    base = {"db": str(db), "end": None, "cache_dir": tmp_path / "cache", "out": None, "smoke": False, "prefetch": False}
    return argparse.Namespace(**{**base, "allow_late": False, "top_n": 100, "min_account": 500_000.0, **kw})


class _Reached(Exception):
    pass


async def _no_network(*a, **k):
    raise _Reached


def test_recount_refuses_before_the_period_end_and_past_the_count_deadline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(r, "fetch_universe", _no_network)
    now = int(time.time())
    early = tmp_path / "early.sqlite"
    _store(early, [], [(now - DAY, "a", 1)])
    for kw in ({}, {"prefetch": True}):
        assert asyncio.run(r.recount_main(_ns(early, tmp_path, **kw))) == 4
    out = capsys.readouterr().out
    assert out.count("REFUSED: the period has not ended") == 2 and "COMPLETENESS" not in out
    with pytest.raises(_Reached):  # a smoke may run any time (it prints structural checks only)
        asyncio.run(r.recount_main(_ns(early, tmp_path, smoke=True)))
    assert asyncio.run(r.recount_main(_ns(early, tmp_path, end="2026-11-25T00:00:00Z"))) == 4
    none = tmp_path / "none.sqlite"
    _store(none, [], [])
    assert asyncio.run(r.recount_main(_ns(none, tmp_path))) == 4
    late = tmp_path / "late.sqlite"
    _store(late, [], [(now - 50 * DAY, "a", 1)])
    assert asyncio.run(r.recount_main(_ns(late, tmp_path))) == 4
    with pytest.raises(_Reached):
        asyncio.run(r.recount_main(_ns(late, tmp_path, allow_late=True)))
    with pytest.raises(_Reached):  # a late prefetch only warns (it can only lose candles by waiting)
        asyncio.run(r.recount_main(_ns(late, tmp_path, prefetch=True)))
    assert "WARNING: prefetch past its registered deadline" in capsys.readouterr().out


# --- the history path ignores the markers ---------------------------------------------------------------------------


def _fake_universe(start, end, short=()):
    days = range(start // DAY * DAY, end + DAY, DAY)
    marks = {c: ([t for t in range(start - DAY, end, 900)], [100.0] * len(range(start - DAY, end, 900))) for c in ("BTC", "ETH")}
    return {
        "top_by_day": dict.fromkeys(days, frozenset({"BTC", "ETH"})),
        "union": ["BTC", "ETH"],
        "marks": marks,
        "short": list(short),
        "failed15": [],
    }


async def _universe(smoke, cache, start, end, runs, held):
    return _fake_universe(start, end)


def test_history_path_does_not_read_the_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "fetch_universe", _universe)
    d = (P - 3 * H) // DAY * DAY
    snap = [(time.strftime("%Y-%m-%d", time.gmtime(d - DAY)), "hyperliquid", acc, d - DAY + 60, 1e6, 1.0, 1.0) for acc in "abd"]
    # the registered default invocation (no --end) on a synthetic END after the first marker
    monkeypatch.setattr(r, "END", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(P + 4 * H)))
    outs = []
    for name, polls in (("without", []), ("with", POLLS)):
        db = tmp_path / f"{name}.sqlite"
        _store(db, POSITIONS, polls, snap)
        out = tmp_path / f"{name}.json"
        monkeypatch.setattr(sys, "argv", ["x", "--db", str(db), "--cache-dir", str(tmp_path), "--out", str(out)])
        assert asyncio.run(r.main()) == 0
        outs.append(json.loads(out.read_text()))
    assert outs[0] == outs[1] and "recount" not in outs[0] and outs[0]["runs"] == 4


def test_history_refuses_an_end_past_the_registered_end_in_every_mode(tmp_path, monkeypatch, capsys):
    """No history run may print gates (or runs, exposure, gaps) on the recount period before its count."""
    monkeypatch.setattr(r, "fetch_universe", _no_network)  # a refusal reads nothing, not even the store
    later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r._ts(r.END) + 1))
    for mode in ([], ["--smoke"], ["--prefetch"]):
        argv = ["x", "--db", str(tmp_path / "absent.sqlite"), "--cache-dir", str(tmp_path), "--end", later, *mode]
        monkeypatch.setattr(sys, "argv", argv)
        assert asyncio.run(r.main()) == 4
    out = capsys.readouterr().out
    assert out.count("REFUSED: --end") == 3 and "GATE" not in out and "OUTCOME" not in out and "runs=" not in out
    # the registered end itself (explicit or the default) is the registered run, not refused
    db = tmp_path / "s.sqlite"
    _store(db, [(r._ts(r.END) - H, "a", "BTC", "short", BIG, 103.0)], [])
    for extra in ([], ["--end", r.END]):
        monkeypatch.setattr(sys, "argv", ["x", "--db", str(db), "--cache-dir", str(tmp_path), *extra])
        with pytest.raises(_Reached):
            asyncio.run(r.main())


# --- the recount's counting branch end to end on a time-shifted synthetic store (candles stubbed) ------------------


def _shifted_store(path, mode, ended_days_ago=1):
    """Hourly runs over a 42-day period that ended ``ended_days_ago`` days ago. a: BTC short $4M (40 000 x mark 100),
    liq_px written at every run, 3 % from the mark (in the band) in the first 3 h of each period day, else 10 %;
    b: ok markers, no rows ever (a flat book). 'incomplete': a's markers are not ok and it has no rows from day 30.
    'flat': only b and c (both markers only). Returns (start, end, number of runs)."""
    now = int(time.time())
    p = (now - (42 + ended_days_ago) * DAY) // H * H + 15 * 60
    end = p + 42 * DAY
    pos = [(p - 2 * H, "a", "BTC", "short", BIG, 110.0), (p - H, "a", "ETH", "closed", 0.0, None)] if mode != "flat" else []
    polls = []
    for t in range(p, end + 2 * H, H):  # also two runs past the end (excluded)
        day, ok_a = (t - p) // DAY, 1
        if mode == "incomplete" and day >= 30:
            ok_a = 0
        if mode == "flat":
            polls += [(t, "b", 1), (t, "c", 1)]
            continue
        polls += [(t, "a", ok_a), (t, "b", 1)]
        if ok_a:
            pos.append((t, "a", "BTC", "short", BIG, 103.0 if (t - p) % DAY < 3 * H else 110.0))
    _store(path, pos, polls)
    return p, end, 42 * 24


def _count(db, tmp_path, monkeypatch, capsys, universe=_universe, **kw):
    monkeypatch.setattr(r, "fetch_universe", universe)
    out = tmp_path / f"{Path(db).stem}.json"
    code = asyncio.run(r.recount_main(_ns(db, tmp_path, out=out, **kw)))
    text = capsys.readouterr().out
    return code, text, (json.loads(out.read_text()) if out.exists() else None)


def test_recount_count_end_to_end_complete(tmp_path, monkeypatch, capsys):
    db = tmp_path / "complete.sqlite"
    start, end, n = _shifted_store(db, "complete")
    code, out, res = _count(db, tmp_path, monkeypatch, capsys)
    assert code == 0
    assert "COMPLETENESS: polled=100.00%" in out and "-> complete" in out and "\nvariant" in out and "GATE" in out
    # observed by the markers from polls_from on: a and b (markers, no rows ever) at every run of the period
    c = res["counters"]
    assert res["polls_from"] == start and res["period"] == [start, end] and res["runs"] == n
    assert c["observed_runs"] == 2 * n and c.get("unpolled_runs", 0) == 0 and c.get("rows_unmarked", 0) == 0
    assert c["in_scope"] == n and c["in_scope_fresh"] == n
    assert res["completeness"]["complete"] is True and res["late"] is False and res["allow_late"] is False
    assert res["short"] == [] and res["failed15"] == [] and res["first15_cached"] == [] and res["union"] == 2
    # period day 0 opens in the band: 'start' (excluded from the gate), then one entry a day on days 1..41. Lower
    # bound, 24 h: the entry interval of day k opens 1 h before 24 h after the counted start of day k-1, so it is
    # merged into it; every second day counts (days 2, 4, .., 40): 20. Without a refractory all 41 count.
    gate = res["variants"]["fresh_1_4|24h|gate"]
    assert res["variants"]["fresh_1_4|24h|start"]["n"] == 1 and gate["n"] == 20
    assert res["variants"]["fresh_1_4|0h|gate"]["n"] == 41
    assert {e["kind"] for e in gate["episodes"]} == {"fresh"}
    # 20 episodes (and 20 coin-days) in 6 weeks: 3.33 / week but 4.76 < 5 per 10 days -> both gates fail -> kill
    assert res["exposure_s"] == pytest.approx(42 * DAY)
    assert res["decision"]["episodes"]["per_10d"] == pytest.approx(20 / 6 * 10 / 7)
    assert res["decision"]["passed"] is False and res["outcome"] == "kill" and "RECOUNT OUTCOME: KILL" in out


def test_recount_count_end_to_end_incomplete_withholds_the_table(tmp_path, monkeypatch, capsys):
    db = tmp_path / "incomplete.sqlite"
    _, _, n = _shifted_store(db, "incomplete")
    code, out, res = _count(db, tmp_path, monkeypatch, capsys)
    assert code == 0
    unpolled = 12 * 24  # a, days 30..41
    assert res["counters"]["observed_runs"] == 2 * n and res["counters"]["unpolled_runs"] == unpolled
    assert res["completeness"]["polled"] == pytest.approx(1 - unpolled / (2 * n))
    assert res["completeness"]["failed"] == ["polled"] and "INCOMPLETE ['polled']" in out
    assert "GATE" not in out and "\nvariant" not in out and res["variants"] == {} and "decision" not in res
    assert res["outcome"] == "incomplete" and "RECOUNT OUTCOME: INCOMPLETE" in out


def test_recount_count_end_to_end_accounts_with_markers_and_no_rows(tmp_path, monkeypatch, capsys):
    db = tmp_path / "flat.sqlite"
    _, _, n = _shifted_store(db, "flat")
    code, out, res = _count(db, tmp_path, monkeypatch, capsys)
    assert code == 0 and res["runs"] == n  # runs from the markers alone
    assert res["counters"]["observed_runs"] == 2 * n and res["counters"].get("unpolled_runs", 0) == 0
    assert res["completeness"]["known"] == 1.0 and res["completeness"]["complete"] is True  # a real absence of whales
    assert res["variants"]["fresh_1_4|24h|gate"]["n"] == 0 and res["outcome"] == "kill" and res["delta_start"] is None


def test_recount_late_count_refuses_incomplete_15m_history_and_records_the_deviation(tmp_path, monkeypatch, capsys):
    db = tmp_path / "flat_late.sqlite"
    _shifted_store(db, "flat", ended_days_ago=9)

    async def short(smoke, cache, start, end, runs, held):
        return _fake_universe(start, end, short=[("BTC", "-")])

    code, out, res = _count(db, tmp_path, monkeypatch, capsys, universe=short, allow_late=True)
    assert code == 4 and "REFUSED: a late count" in out and "COMPLETENESS" not in out and res is None
    code, out, res = _count(db, tmp_path, monkeypatch, capsys, allow_late=True)
    assert code == 0 and res["late"] is True and res["allow_late"] is True and res["started_at"] > 0
    code, out, res = _count(db, tmp_path, monkeypatch, capsys)  # without the flag: the deadline refusal
    assert code == 4


def test_recount_smoke_never_writes_into_the_cache_dir_the_count_uses(tmp_path, monkeypatch, capsys):
    db = tmp_path / "flat.sqlite"
    _shifted_store(db, "flat")
    seen = []

    async def universe(smoke, cache, start, end, runs, held):
        seen.append(cache)
        return _fake_universe(start, end)

    monkeypatch.setattr(r, "fetch_universe", universe)
    assert asyncio.run(r.recount_main(_ns(db, tmp_path, smoke=True))) == 0
    assert asyncio.run(r.recount_main(_ns(db, tmp_path))) == 0
    assert seen == [tmp_path / "cache" / "smoke", tmp_path / "cache"]
    assert "COMPLETENESS" not in capsys.readouterr().out.split("smoke done")[0]
