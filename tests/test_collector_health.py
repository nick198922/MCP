"""Collector health: pure shares and gaps (analytics/collector_health.py) and the read-only script on a fixture DB."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk import features
from tradedesk.analytics import collector_health as ch
from tradedesk.store import Store

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import collector_health as script  # noqa: E402

H, D = ch.HOUR, ch.DAY
NOW = int(datetime(2026, 11, 2, 6, 0, tzinfo=UTC).timestamp())  # the brief's hour (08:00 Kyiv)
SPEC = {s.name: s for s in ch.SPECS}


def _hourly(first: int, last: int, skip: set[int] = frozenset()) -> list[int]:
    """Stamps 7 min into every hour from ``first`` to ``last`` (hour starts), leaving out the hours in ``skip``."""
    return [h + 420 for h in range(first, last + 1, H) if h not in skip]


# ------------------------------------------------------------------------------------------------------ pure


def test_full_hourly_series_is_ok():
    stamps = _hourly(NOW - 8 * D, NOW - H)
    r = ch.series_health(SPEC["pm_gap"], "", stamps, stamps[0], stamps[-1], NOW)
    assert (r["status"], r["expected"], r["present"], r["gaps"], r["trailing_gap_s"]) == ("ok", 168, 168, 0, 0)
    assert r["share"] == pytest.approx(1.0) and r["since"] == NOW - 7 * D


def test_gap_in_the_middle_lowers_the_share_and_is_measured():
    hole = {NOW - 3 * D + i * H for i in range(20)}  # 20 h missing three days ago
    stamps = _hourly(NOW - 8 * D, NOW - H, hole)
    r = ch.series_health(SPEC["pm_gap"], "", stamps, stamps[0], stamps[-1], NOW)
    assert r["status"] == "low" and (r["present"], r["expected"]) == (148, 168)
    assert r["share"] == pytest.approx(148 / 168)
    assert (r["gaps"], r["longest_gap_s"], r["longest_gap_start"]) == (1, 20 * H, NOW - 3 * D)


def test_fresh_trailing_gap_is_named_even_with_a_good_share():
    stamps = _hourly(NOW - 8 * D, NOW - 4 * H)  # the last 3 hours missing: share 165/168 = 98 %
    r = ch.series_health(SPEC["hl_oi_cap"], "", stamps, stamps[0], stamps[-1], NOW)
    assert r["share"] > ch.THRESHOLD and r["status"] == "stale" and r["trailing_gap_s"] == 3 * H
    two = _hourly(NOW - 8 * D, NOW - 3 * H)  # 2 hours: not yet
    assert ch.series_health(SPEC["hl_oi_cap"], "", two, two[0], two[-1], NOW)["status"] == "ok"


def test_window_starts_at_the_first_row_and_new_or_empty_series():
    stamps = _hourly(NOW - 10 * H, NOW - H)  # switched on 10 hours ago
    r = ch.series_health(SPEC["flow_snapshot"], "", stamps, stamps[0], stamps[-1], NOW)
    assert (r["status"], r["expected"], r["since"]) == ("ok", 10, NOW - 10 * H)
    assert ch.series_health(SPEC["flow_snapshot"], "", [], None, None, NOW)["status"] == "never"
    just = NOW + 300  # first row 5 minutes ago: its hour is not due yet
    assert ch.series_health(SPEC["flow_snapshot"], "", [just], just, just, NOW + 600)["status"] == "new"


def test_option_xvenue_expects_no_hours_from_06_to_11_utc():
    spec = SPEC["option_xvenue"]
    exp = ch.expected_buckets(spec, NOW - 7 * D, NOW)
    assert len(exp) == 7 * 18 and not any(6 <= (b % D) // H <= 11 for b in exp)
    stamps = [b + 1200 for b in exp]
    assert ch.series_health(spec, "BTC", stamps, stamps[0], stamps[-1], NOW)["share"] == pytest.approx(1.0)


def test_daily_series_count_a_day_only_once_it_is_due():
    legs = SPEC["option_legs"]  # from 09:00 UTC, due by 10:00
    today = NOW - NOW % D
    days = [today - i * D for i in range(1, 9)]
    assert ch.series_health(legs, "BTC", days, days[-1], days[0], NOW)["status"] == "ok"  # 06:00: today not due
    r = ch.series_health(legs, "BTC", days, days[-1], days[0], today + 10 * H)
    assert (r["status"], r["present"], r["expected"], r["trailing_gap_s"]) == ("low", 6, 7, D)  # one missed day = 85 %
    cyc = SPEC["cycle_regime_log"]  # yesterday's closed day due from 01:00 UTC today
    r = ch.series_health(cyc, "BTC", days[1:], days[-1], days[1], NOW)
    assert (r["status"], r["trailing_gap_s"]) == ("low", D)
    assert ch.series_health(cyc, "BTC", days[1:], days[-1], days[1], today + H - 1)["status"] == "ok"  # not due yet


def test_paper_carry_cycles_every_8_hours():
    spec = SPEC["paper_carry"]
    exp = ch.expected_buckets(spec, NOW - 7 * D, NOW)
    assert len(exp) == 21 and all((b % D) // H in (0, 8, 16) for b in exp)


def test_brief_line_names_the_worst_first_and_caps_the_list():
    ok = {"label": "a", "status": "ok", "share": 1.0}
    assert ch.brief_line([ok]) is None
    low = {"label": "pm_gap", "status": "low", "share": 0.899, "period_s": H, "gaps": 2, "longest_gap_s": 6 * H}
    line = ch.brief_line([ok, low | {"last_ts": NOW - H}])
    assert line.startswith("Колектори: частка знімків за 7 дн нижче 90 %")
    assert "pm_gap 89 % (дір 2, найдовша 6 год; останній 02.11 05:00 UTC)" in line  # floor: 0.899 is not 90 %
    many = [low | {"label": f"s{i}", "share": 0.5 + i / 100, "last_ts": None} for i in range(5)]
    err = {"label": "radar_archive:BTC", "status": "error", "error": "запит не вдався: OperationalError", "share": None}
    line = ch.brief_line([*many, err])
    assert line.index("radar_archive:BTC не перевірено") < line.index("s0 50 %") and line.endswith("; і ще 2.")
    never = {"label": "option_legs:ETH", "status": "never", "share": None}
    assert "option_legs:ETH: жодного запису" in ch.brief_line([never])


def test_every_switched_on_job_is_checked_or_says_why_not():
    switches, rejected = features.parse((ROOT / "deploy" / "features.env").read_text(encoding="utf-8"))
    assert not rejected
    on = {k for k, v in switches.items() if k.endswith("_ENABLED") and v == "1"}
    flags = {s.flag for s in ch.SPECS}
    assert on - flags - set(ch.NOT_CHECKED) == set(), "a new job in features.env needs a Spec or a NOT_CHECKED reason"
    assert {s.name for s in ch.enabled_specs(switches)} == {s.name for s in ch.SPECS if s.flag in on or s.default_on}
    from tradedesk.providers.exchanges import PERP_EXCHANGES

    assert sorted(SPEC["funding"].groups) == sorted((*PERP_EXCHANGES, "hyperliquid", "deribit"))  # _collect_funding
    venues = {"binance": "binanceusdm"}  # ccxt id the liquidation rows carry
    liq = [venues.get(v, v) for v in switches["TRADEDESK_COLLECT_LIQUIDATIONS_VENUES"].split(",")]
    assert sorted(SPEC["liquidations"].groups) == sorted(liq)
    from tradedesk.collector.coinalyze_history import venue_symbols

    assert sorted(SPEC["coinalyze_history"].groups) == sorted(venue_symbols(switches["TRADEDESK_COLLECT_COINALYZE_HISTORY_VENUES"])[0])


def test_render_lists_every_series():
    stamps = _hourly(NOW - 8 * D, NOW - H, {NOW - 2 * D})
    r = ch.series_health(SPEC["pm_gap"], "", stamps, stamps[0], stamps[-1], NOW)
    text = ch.render([r, ch.error_health(SPEC["flow_snapshot"], "", "x")], NOW)
    assert "✓ pm_gap: 99 % (167/168 з 26.10 06:00 UTC), дір 1, найдовша 1 год з 31.10 06:00 UTC" in text
    assert "✗ flow_snapshot не перевірено (x)" in text and "У брифі: Колектори:" in text


# ------------------------------------------------------------------------------------------------------ script


def _fresh_db(path: Path) -> str:
    db = str(path / "fixture.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    asyncio.run(go())
    return db


def test_every_spec_query_runs_on_the_real_schema(tmp_path):
    db = _fresh_db(tmp_path)
    switches = {s.flag: "1" for s in ch.SPECS}
    results = script.collect(db, switches, NOW)
    assert len(results) == sum(len(s.groups) for s in ch.SPECS)
    assert {r["status"] for r in results} == {"never"}  # no column typo: every query ran


def test_script_on_a_fixture_db(tmp_path):
    db = _fresh_db(tmp_path)
    con = sqlite3.connect(db)
    pm = "INSERT INTO pm_gap_snapshot (ts, market_id, asset, kind, direction, strike, end_ts, hours_to_end) VALUES (?,?,?,?,?,?,?,?)"
    for ts in _hourly(NOW - 8 * D, NOW - H, {NOW - 3 * D + i * H for i in range(30)}):  # 30 h hole
        con.execute(pm, (ts, "m1", "BTC", "above", "above", 1e5, ts + D, 24.0))
        con.execute(pm, (ts, "m2", "ETH", "above", "above", 4e3, ts + D, 24.0))  # several rows a snapshot: one bucket
    liq = "INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd) VALUES (?,?,?,?,?,?,?)"
    for venue in ("binanceusdm", "bybit", "okx"):
        for ts in _hourly(NOW - 8 * D, NOW - H):
            con.execute(liq, (ts, venue, "BTC/USDT:USDT", "sell", 1.0, 1e5, 1e5))
    for b in range(NOW - 6 * H - 8 * D, NOW, 8 * H):  # 00/08/16 UTC cycles, ms
        con.execute("INSERT INTO cp_cycle (strategy, cycle_ts, status) VALUES ('A', ?, 'done')", (b * 1000,))
        status = "failed" if b == NOW - 6 * H else "done"  # B: today's 00:00 cycle failed
        con.execute("INSERT INTO cp_cycle (strategy, cycle_ts, status) VALUES ('B', ?, ?)", (b * 1000, status))
    con.commit()
    con.close()
    switches = {"TRADEDESK_PM_GAP_ENABLED": "1", "TRADEDESK_PAPER_CARRY_ENABLED": "1", "TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED": "1"}
    switches |= {s.flag: "0" for s in ch.SPECS if s.default_on}  # the default jobs have their own test below
    res = {r["label"]: r for r in script.collect(db, switches, NOW)}
    assert set(res) == {"pm_gap", "paper_carry:A", "paper_carry:B", "liquidations:binanceusdm", "liquidations:bybit", "liquidations:okx"}
    assert res["pm_gap"]["status"] == "low" and (res["pm_gap"]["present"], res["pm_gap"]["expected"]) == (138, 168)
    assert res["pm_gap"]["longest_gap_s"] == 30 * H and res["pm_gap"]["last_ts"] == NOW - H + 420
    assert res["liquidations:okx"]["status"] == "ok" and res["paper_carry:A"]["status"] == "ok"
    b = res["paper_carry:B"]  # the 00:00 cycle failed: the latest due cycle is missing
    assert b["status"] == "stale" and b["trailing_gap_s"] == 8 * H and b["present"] == b["expected"] - 1
    features_file = tmp_path / "features.env"
    features_file.write_text("\n".join(f"{k}={v}" for k, v in switches.items()) + "\n", encoding="utf-8")
    line = script.brief_line(db, features_file, NOW)
    assert line.startswith("Колектори:") and "pm_gap 82 %" in line and "paper_carry:B 95 % (немає 8 год поспіль" in line
    out = tmp_path / "r" / "health.txt"
    assert script.main(["--db", db, "--features", str(features_file), "--out", str(out)]) == 0
    assert "pm_gap" in out.read_text(encoding="utf-8")


def test_default_jobs_count_without_a_switch():
    defaults = {"atm_term", "basis", "funding", "supply", "traders", "trader_positions"}
    assert {s.name for s in ch.enabled_specs({})} == defaults  # nothing in features.env: only the default jobs
    off = {"TRADEDESK_COLLECT_TRADERS_ENABLED": "0", "TRADEDESK_PM_GAP_ENABLED": "1"}
    assert {s.name for s in ch.enabled_specs(off)} == defaults - {"traders", "trader_positions"} | {"pm_gap"}
    assert {s.name for s in ch.enabled_specs({"TRADEDESK_COLLECT_SUPPLY_ENABLED": "1"})} == defaults


def test_traders_gap_in_one_venue_is_visible_and_empty_db(tmp_path):
    db = _fresh_db(tmp_path)
    only_traders = {"TRADEDESK_COLLECTOR_ENABLED": "0", "TRADEDESK_COLLECT_SUPPLY_ENABLED": "0"}  # traders: no switch
    empty = {r["label"]: r["status"] for r in script.collect(db, only_traders, NOW)}
    labels = {"traders:hyperliquid", "traders:polymarket", "traders:polymarket_crypto"}
    labels |= {"trader_positions:hyperliquid", "trader_positions:polymarket"}
    assert empty == dict.fromkeys(labels, "never")
    con = sqlite3.connect(db)
    today = NOW - NOW % D
    snap = "INSERT INTO trader_snapshot (day, venue, account, ts) VALUES (?,?,?,?)"
    for k in range(1, 9):  # 25.10 .. 01.11; Polymarket (overall) silent from 30.10, as after its v1 shutdown
        day = today - k * D
        iso = datetime.fromtimestamp(day, UTC).strftime("%Y-%m-%d")
        for venue in ("hyperliquid", "polymarket", "polymarket_crypto"):
            if venue != "polymarket" or k > 3:
                con.executemany(snap, [(iso, venue, f"0x{i}", day + 4800) for i in range(3)])
    pos = "INSERT INTO trader_positions (ts, venue, account, market, side) VALUES (?,?,?,?,?)"
    for ts in _hourly(NOW - 8 * D, NOW - H):
        con.execute(pos, (ts, "hyperliquid", "0xa", "BTC", "long"))
    for ts in _hourly(NOW - 8 * D, NOW - 3 * D - H):  # Polymarket positions stop 72 h ago
        con.execute(pos, (ts, "polymarket", "0xb", "123", "Yes"))
    con.commit()
    con.close()
    res = {r["label"]: r for r in script.collect(db, only_traders, NOW)}
    assert set(res) == labels
    assert res["traders:hyperliquid"]["status"] == "ok" and res["traders:polymarket_crypto"]["status"] == "ok"
    assert (res["traders:hyperliquid"]["present"], res["traders:hyperliquid"]["expected"]) == (6, 6)  # 27.10 .. 01.11
    pm = res["traders:polymarket"]
    assert (pm["status"], pm["present"], pm["expected"], pm["trailing_gap_s"]) == ("low", 3, 6, 3 * D)
    assert res["trader_positions:hyperliquid"]["status"] == "ok"
    pp = res["trader_positions:polymarket"]
    assert (pp["status"], pp["present"], pp["expected"], pp["trailing_gap_s"]) == ("low", 96, 168, 72 * H)
    line = ch.brief_line(res.values())
    assert "traders:polymarket 50 % (немає 3.0 дн поспіль; останній 29.10)" in line
    assert "trader_positions:polymarket 57 %" in line and "hyperliquid" not in line


def test_script_unreadable_db_or_table(tmp_path):
    line = script.brief_line(str(tmp_path / "none.sqlite"), ROOT / "deploy" / "features.env", NOW)
    assert line == "Здоров'я колекторів не перевірено: БД не відкрито (FileNotFoundError)."
    bare = tmp_path / "bare.sqlite"
    sqlite3.connect(bare).close()  # a DB without the collector tables
    res = script.collect(str(bare), {"TRADEDESK_PM_GAP_ENABLED": "1"}, NOW)
    assert res[0]["status"] == "error" and "OperationalError" in res[0]["error"]
    assert "pm_gap не перевірено" in script.brief_line(str(bare), _features(tmp_path), NOW)
    assert script.main(["--db", str(tmp_path / "none.sqlite")]) == 1


def _features(tmp_path: Path) -> Path:
    p = tmp_path / "f.env"
    p.write_text("TRADEDESK_PM_GAP_ENABLED=1\n", encoding="utf-8")
    return p
