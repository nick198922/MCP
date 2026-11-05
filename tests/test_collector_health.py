"""Collector health: pure shares and gaps (analytics/collector_health.py) and the read-only script on a fixture DB."""

from __future__ import annotations

import asyncio
import json
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


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot(path: Path, asof: int) -> Path:
    path.write_text(json.dumps({"schema": 1, "asof_utc": _iso(asof), "sections": {}}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _fresh_assistant_snapshot(tmp_path, monkeypatch):
    """brief_line reads the server's snapshot file by default: point it at a fresh one, so only the DB decides."""
    monkeypatch.setattr(script, "SNAPSHOT_PATH", _snapshot(tmp_path / "snapshot.json", NOW - 60))


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


def test_a_series_that_wrote_again_has_a_closed_gap_not_a_trailing_one():
    # Live 05.11 08:00 UTC: Polymarket leaderboards missed 03.11 and 04.11, the v2 fix wrote 05.11 (not due yet), and the
    # brief still said "немає 2.0 дн поспіль". The run is a closed gap once a later snapshot exists.
    traders = SPEC["traders"]  # daily, due by the end of the day
    today = NOW - NOW % D
    now = today + 8 * H
    days = [today - k * D + 4800 for k in (8, 7, 6, 5, 4, 3)]
    stopped = ch.series_health(traders, "polymarket", days, days[0], days[-1], now)
    assert (stopped["status"], stopped["present"], stopped["expected"], stopped["trailing_gap_s"]) == ("low", 4, 6, 2 * D)
    assert "немає 2.0 дн поспіль" in ch.describe(stopped)  # still silent: named as missing now
    back = [*days, today + 4800]
    r = ch.series_health(traders, "polymarket", back, back[0], back[-1], now)
    assert (r["status"], r["present"], r["expected"], r["trailing_gap_s"]) == ("low", 4, 6, 0)  # the share is still low
    assert (r["gaps"], r["longest_gap_s"], r["longest_gap_start"]) == (1, 2 * D, today - 2 * D)
    assert ch.describe(r) == "traders:polymarket 66 % (дір 1, найдовша 2.0 дн; останній 02.11)"
    carry = SPEC["paper_carry"]  # 8 h cycles: the 00:00 cycle failed, the 08:00 one is done (due only from 10:00)
    cycles = list(range(today - 7 * D, today, 8 * H))
    assert ch.series_health(carry, "B", cycles, cycles[0], cycles[-1], today + 9 * H)["status"] == "stale"
    later = [*cycles, today + 8 * H + 900]
    r = ch.series_health(carry, "B", later, later[0], later[-1], today + 9 * H)
    assert (r["status"], r["trailing_gap_s"], r["gaps"]) == ("ok", 0, 1)
    hourly = _hourly(NOW - 8 * D, NOW - 4 * H)  # the last 3 due hours missing, but the current hour has a row
    r = ch.series_health(SPEC["hl_oi_cap"], "", [*hourly, NOW + 420], hourly[0], NOW + 420, NOW + 600)
    assert (r["status"], r["trailing_gap_s"], r["gaps"], r["longest_gap_s"]) == ("ok", 0, 1, 3 * H)


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
    checked = flags | set(ch.NOT_CHECKED) | {ch.SNAPSHOT_FLAG}  # the snapshot file is checked by its age
    assert on - checked == set(), "a new job in features.env needs a Spec or a NOT_CHECKED reason"
    assert {s.name for s in ch.enabled_specs(switches)} == {s.name for s in ch.SPECS if s.flag in on or s.default_on}
    from tradedesk.providers.exchanges import PERP_EXCHANGES

    assert sorted(SPEC["funding"].groups) == sorted((*PERP_EXCHANGES, "hyperliquid", "deribit"))  # _collect_funding
    venues = {"binance": "binanceusdm"}  # ccxt id the liquidation rows carry
    liq = [venues.get(v, v) for v in switches["TRADEDESK_COLLECT_LIQUIDATIONS_VENUES"].split(",")]
    assert sorted(SPEC["liquidations"].groups) == sorted(liq)
    from tradedesk.collector.coinalyze_history import venue_symbols

    assert sorted(SPEC["coinalyze_history"].groups) == sorted(venue_symbols(switches["TRADEDESK_COLLECT_COINALYZE_HISTORY_VENUES"])[0])


# Collector jobs without a Spec, and why (item 96 of the standing card; docs/BACKLOG.md 6.13). These jobs are not in
# the brief's health line. A new collector job needs a Spec in analytics/collector_health.SPECS or a reason here.
EXEMPT: dict[str, str] = {
    "retention": "maintenance sweep: deletes rows older than the retention windows, stores no snapshot",
    "alerts": "event-driven: writes an alert row only when a rule or a provider-health transition fires, no cadence",
    "assistant_snapshot": "writes a JSON file (assistant/snapshot.json), not a table, so no Spec; now checked by the "
    "age of its asof_utc (analytics/collector_health.snapshot_line, scripts/collector_health.snapshot_check)",
    "ohlcv_hist": "off (no line in features.env; its markets come from the service env), so no fixed series; a bar "
    "backfill that refills holes from the last stored bar. Switched on in features.env, it fails "
    "test_every_switched_on_job_is_checked_or_says_why_not until it gets a Spec",
}
NOT_A_JOB = {"TRADEDESK_ALERTS_HEALTH_ENABLED": "sub-switch of the alerts job (provider-health alerts), registers no job"}


def _jobs(monkeypatch, env: dict[str, str]) -> set[str]:
    """Names of the jobs build_default_scheduler registers under ``env`` ('liquidations:bybit' -> 'liquidations')."""
    from types import SimpleNamespace

    from tradedesk.collector.scheduler import build_default_scheduler
    from tradedesk.config import Settings

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sched = build_default_scheduler(SimpleNamespace(settings=Settings.from_env(), store=object()))
    return set() if sched is None else {j.name.split(":")[0] for j in sched.jobs}


def test_every_collector_job_has_a_spec_or_an_exemption(monkeypatch):
    import re

    src = ROOT / "src" / "tradedesk"
    switches = sorted(set(re.findall(r'"(TRADEDESK_[A-Z0-9_]+_ENABLED)"', (src / "config.py").read_text(encoding="utf-8"))))
    registered = set(re.findall(r'PeriodicJob\(\s*name=f?"([a-z_]+)', (src / "collector" / "scheduler.py").read_text(encoding="utf-8")))
    on = dict.fromkeys(switches, "1") | {
        "TRADEDESK_COLLECT_OHLCV_HIST_MARKETS": "binanceusdm:BTC/USDT:USDT:1h",  # the job needs a market to register
        "TRADEDESK_COLLECT_LIQUIDATIONS_VENUES": "binance,bybit,okx",
    }
    jobs = _jobs(monkeypatch, on)
    assert jobs == registered  # with every switch on, every registration in scheduler.py is reached
    controls = {sw: jobs - _jobs(monkeypatch, on | {sw: "0"}) for sw in switches}  # jobs each switch turns off
    owner = {j: min((sw for sw in switches if j in controls[sw]), key=lambda sw: len(controls[sw])) for j in jobs}
    flag = {s.name: s.flag for s in ch.SPECS}
    gaps = sorted(j for j in jobs if j not in flag and owner[j] not in ch.NOT_CHECKED and j not in EXEMPT)
    assert gaps == [], "a collector job needs a Spec in analytics/collector_health.SPECS or an EXEMPT reason here"
    assert set(flag) <= jobs and {j: owner[j] for j in flag} == flag  # each Spec names a real job and its own switch
    assert set(EXEMPT) <= jobs - set(flag)  # no stale exemption
    assert set(ch.NOT_CHECKED) <= {owner[j] for j in jobs}
    assert [sw for sw in switches if not controls[sw] and sw not in NOT_A_JOB] == []  # a switch without a job is named
    assert set(NOT_A_JOB) <= set(switches)


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


# ------------------------------------------------------------------------------------------------ assistant snapshot


def test_snapshot_line_fresh_stale_missing_and_unreadable():
    cad = ch.SNAPSHOT_DEFAULT_S  # 600 s: stale after 20 min
    assert ch.snapshot_line(_iso(NOW - 20 * 60), NOW, cad) is None  # exactly 2 cadences: still fresh
    assert ch.snapshot_line(_iso(NOW + 30), NOW, cad) is None  # a stamp slightly ahead of the clock
    stale = ch.snapshot_line(_iso(NOW - 3 * H), NOW, cad)
    assert stale == "Знімок асистента застарів: останній 02.11 03:00 UTC, 3 год тому (оновлюється кожні 10 хв)."
    assert ch.snapshot_line(_iso(NOW - 20 * 60 - 1), NOW, cad).startswith("Знімок асистента застарів")
    assert ch.snapshot_line(_iso(NOW - 50 * 60), NOW, 1800) is None  # a slower cadence moves the threshold
    assert ch.snapshot_line(None, NOW, cad, exists=False) == "Знімок асистента: файлу немає."
    for bad in (None, "", "02.11.2026", 123):
        assert ch.snapshot_line(bad, NOW, cad) == "Знімок асистента: час знімка не прочитано."


def test_snapshot_cadence_follows_features_env():
    assert ch.snapshot_cadence({}) == 600  # config.Settings.assistant_snapshot_seconds
    assert ch.snapshot_cadence({ch.SNAPSHOT_FLAG: "1", ch.SNAPSHOT_SECONDS_FLAG: "900"}) == 900
    assert ch.snapshot_cadence({ch.SNAPSHOT_SECONDS_FLAG: "x"}) == 600
    assert ch.snapshot_cadence({ch.SNAPSHOT_FLAG: "0"}) is None


def test_scheduler_runs_the_snapshot_at_the_cadence_the_check_assumes(monkeypatch):
    from types import SimpleNamespace

    from tradedesk.collector.scheduler import build_default_scheduler
    from tradedesk.config import Settings

    monkeypatch.setenv("TRADEDESK_COLLECTOR_ENABLED", "1")
    monkeypatch.setenv(ch.SNAPSHOT_FLAG, "1")
    monkeypatch.delenv(ch.SNAPSHOT_SECONDS_FLAG, raising=False)
    sched = build_default_scheduler(SimpleNamespace(settings=Settings.from_env(), store=object()))
    assert [j.period_seconds for j in sched.jobs if j.name == "assistant_snapshot"] == [ch.SNAPSHOT_DEFAULT_S]


def test_snapshot_check_reads_the_file(tmp_path):
    fresh = _snapshot(tmp_path / "a.json", NOW - 5 * 60)
    assert script.snapshot_check(fresh, {}, NOW) is None
    stale = _snapshot(tmp_path / "b.json", NOW - 2 * H)
    assert script.snapshot_check(stale, {}, NOW).startswith("Знімок асистента застарів: останній 02.11 04:00 UTC")
    assert script.snapshot_check(stale, {ch.SNAPSHOT_FLAG: "0"}, NOW) is None  # switched off in features.env
    assert script.snapshot_check(tmp_path / "none.json", {}, NOW) == "Знімок асистента: файлу немає."
    corrupt = tmp_path / "c.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert script.snapshot_check(corrupt, {}, NOW) == "Знімок асистента: час знімка не прочитано."
    assert script.snapshot_check(tmp_path, {}, NOW) == "Знімок асистента: час знімка не прочитано."  # a directory


def test_brief_line_adds_the_snapshot_only_when_missing_or_stale(tmp_path):
    db = _fresh_db(tmp_path)
    quiet = tmp_path / "quiet.env"
    quiet.write_text("\n".join(f"{s.flag}=0" for s in ch.SPECS if s.default_on) + "\n", encoding="utf-8")  # no series
    assert script.brief_line(db, quiet, NOW) is None  # the fixture's fresh snapshot
    stale = _snapshot(tmp_path / "old.json", NOW - H)
    assert script.brief_line(db, quiet, NOW, stale).startswith("Знімок асистента застарів: останній 02.11 05:00 UTC")
    missing = tmp_path / "gone.json"
    assert script.brief_line(db, quiet, NOW, missing) == "Знімок асистента: файлу немає."
    line = script.brief_line(db, _features(tmp_path), NOW, missing)  # a DB alarm and the snapshot: one line, DB first
    assert line.startswith("Колектори:") and line.endswith(". Знімок асистента: файлу немає.")
    line = script.brief_line(str(tmp_path / "none.sqlite"), quiet, NOW, missing)  # the snapshot is checked without a DB
    assert line == "Здоров'я колекторів не перевірено: БД не відкрито (FileNotFoundError). Знімок асистента: файлу немає."
