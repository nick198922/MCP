"""Item 313: the «S12: правило зняття» section of the checkpoint report (owner decision Q-20261109-1, option 1).

Pure rule in ``analytics/checkpoint.s12_stop_rule``; the script reads a fake collector DB read-only, no network."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from tradedesk.analytics import checkpoint as cp
from tradedesk.analytics.s12_demo_audit import PASSPORTS
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import checkpoint_report as script  # noqa: E402

H = 3_600_000
DAY = 86_400_000
V9 = cp._ms(2026, 11, 1)  # after the v9 epoch (2026-10-31 09:30 UTC)
V4 = cp._ms(2026, 10, 26)  # v4 epoch, before v7 (2026-10-27 18:00 UTC)
NOW = cp._ms(2026, 12, 20)


def _s12(i: int, r: float, *, opened: int, version: str | None = "v9", **kw) -> dict:
    o = opened + i * 5 * H  # about five trades a day: several signal days for the clustered CI
    row = {"link_id": f"S12-{opened}-{i}", "setup": "S12", "timeframe": "4h", "status": "closed", "signal_ts": o - 4 * H}
    return row | {"opened_ts": o, "closed_ts": o + H, "r_multiple": r, "version": version} | kw


def _v9(n: int, r: float) -> list[dict]:
    return [_s12(i, r, opened=V9) for i in range(n)]


def _v4(n: int, r: float) -> list[dict]:  # untagged, no resting limit: the v4 entry (traded_version)
    return [_s12(i, r, opened=V4, version=None) for i in range(n)]


def _check(block: dict, name: str) -> dict:
    return next(c for c in block["checks"] if c["name"] == name)


def test_thresholds_come_from_the_report():
    assert cp.S12_STOP_FLOOR == PASSPORTS["v4"].band_sliding[0] == -0.827  # rolling p5 of the v4 base, n 30
    s12 = next(s for s in cp.DEMO if s.setup == "S12")
    assert s12.bands[30][1][0] == cp.S12_STOP_FLOOR  # the same band as the S12 row of the table
    assert (cp.S12_STOP_N, cp.S12_DROP_N, cp.S12_DROP_FLOOR) == (30, 100, -0.50)


def test_below_both_n_thresholds_is_early_without_a_verdict():
    # the state of the 09.11 report: 26 v9 + 5 untagged v4 trades
    b = cp.s12_stop_rule(_v9(26, -2.0) + _v4(5, -1.0), NOW)
    assert b["verdict"] == cp.EARLY
    v9, v4e = _check(b, "v9"), _check(b, "вхід v4 (v4 + v9)")
    assert (v9["n"], v9["verdict"]) == (26, cp.EARLY) and v9["mean"] == pytest.approx(-2.0)
    assert (v4e["n"], v4e["verdict"]) == (31, cp.EARLY)
    text = "\n".join(cp.s12_stop_rule_lines(b))
    assert "рано (26 з 30), без вердикту" in text and "рано (31 з 100), без вердикту" in text
    assert "зараз: зупинити" not in text and "зараз: зняти" not in text


def test_v9_at_30_below_the_floor_stops_and_above_it_holds():
    stop = cp.s12_stop_rule(_v9(30, -0.84), NOW)
    assert _check(stop, "v9")["verdict"] == cp.S12_STOP == "зупинити й розібрати виконання"
    assert _check(stop, "вхід v4 (v4 + v9)")["verdict"] == cp.EARLY  # 30 of 100
    assert stop["verdict"] == cp.S12_STOP
    hold = cp.s12_stop_rule(_v9(30, -0.82), NOW)
    assert _check(hold, "v9")["verdict"] == cp.S12_HOLD == "тримати (перевагу не доведено)"
    assert hold["verdict"] == cp.S12_HOLD


def test_v4_entry_at_100_below_the_floor_drops():
    # 70 v9 at -0.6 (above the v9 floor: hold) + 30 untagged v4 at -0.6: the v4 entry mean -0.6 < -0.50 -> drop
    b = cp.s12_stop_rule(_v9(70, -0.6) + _v4(30, -0.6), NOW)
    assert _check(b, "v9")["verdict"] == cp.S12_HOLD
    v4e = _check(b, "вхід v4 (v4 + v9)")
    assert v4e["n"] == 100 and v4e["mean"] == pytest.approx(-0.6) and v4e["verdict"] == cp.S12_DROP == "зняти"
    assert b["verdict"] == cp.S12_DROP  # the drop outranks hold
    held = cp.s12_stop_rule(_v9(70, -0.4) + _v4(30, -0.4), NOW)
    assert _check(held, "вхід v4 (v4 + v9)")["verdict"] == cp.S12_HOLD and held["verdict"] == cp.S12_HOLD


def test_v7_other_setups_open_and_future_trades_are_left_out():
    v7 = [_s12(i, 5.0, opened=cp._ms(2026, 10, 28), version=None, rest_until=cp._ms(2026, 10, 29)) for i in range(4)]
    tagged_v7 = [_s12(i, 5.0, opened=cp._ms(2026, 10, 29), version="v7") for i in range(2)]
    other = [_s12(i, 5.0, opened=V9, setup="S3") for i in range(3)]
    opened = [_s12(i, 5.0, opened=V9, status="open", closed_ts=None, r_multiple=None) for i in range(3)]
    later = [_s12(i, 5.0, opened=NOW) for i in range(3)]  # closed after the report moment
    no_r = [_s12(i, 5.0, opened=V9, r_multiple=None) for i in range(2)]
    b = cp.s12_stop_rule(_v9(10, 0.1) + v7 + tagged_v7 + other + opened + later + no_r, NOW)
    assert _check(b, "v9")["n"] == 10 and _check(b, "вхід v4 (v4 + v9)")["n"] == 10
    assert _check(b, "v9")["mean"] == pytest.approx(0.1)
    assert b["other"] == 6  # the v7 trades, named in the text
    assert "з іншим входом (v7 чи версія невідома, не входять): 6" in "\n".join(cp.s12_stop_rule_lines(b))


def test_ci_is_clustered_by_signal_day_and_brackets_the_mean():
    rows = [_s12(i, r, opened=V9) for i, r in enumerate([-1.0, -1.0, 2.5, -1.0, 0.0, -1.0, 3.0, -1.0, -1.0, 0.5] * 3)]
    c = _check(cp.s12_stop_rule(rows, NOW), "v9")
    assert c["n"] == 30 and c["days"] == len({r["signal_ts"] // DAY for r in rows}) >= 5
    lo, hi = c["ci"]
    assert lo < c["mean"] < hi and -1.0 <= lo and hi <= 3.0
    one_day = cp.s12_stop_rule([_s12(0, 1.0, opened=V9 + 6 * H), _s12(1, -1.0, opened=V9 + 6 * H)], NOW)  # signals 02:00, 07:00
    assert _check(one_day, "v9")["ci"] is None  # one signal day: no CI, printed as a dash
    assert "95 % CI —" in "\n".join(cp.s12_stop_rule_lines(one_day))


def test_no_s12_trades_prints_dashes_not_none():
    b = cp.s12_stop_rule([], NOW)
    assert b["verdict"] == cp.EARLY and b["other"] == 0
    text = "\n".join(cp.s12_stop_rule_lines(b))
    assert "n 0" in text and "середній R —" in text and "None" not in text and "nan" not in text.lower()


def _db(tmp_path: Path, rows: list[dict]) -> str:
    db = str(tmp_path / "fixture.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    asyncio.run(go())
    con = sqlite3.connect(db)
    cols = (
        "link_id, venue, symbol, side, setup, timeframe, signal_ts, entry, stop, target, qty, leverage, risk_usd, comment, "
        "status, opened_ts, closed_ts, r_multiple, version"
    )
    fixed = {"venue": "bybit-demo", "symbol": "X/USDT:USDT", "side": "long", "entry": 1, "stop": 0.9, "target": 1.2}
    fixed |= {"qty": 1, "leverage": 5, "risk_usd": 100, "comment": "c"}
    names = [c.strip() for c in cols.split(",")]
    for r in rows:
        v = r | fixed
        con.execute(f"INSERT INTO demo_trade ({cols}) VALUES ({', '.join('?' * len(names))})", [v[k] for k in names])
    con.commit()
    con.close()
    return db


def test_script_prints_the_section_from_a_read_only_db(tmp_path):
    db = _db(tmp_path, _v9(30, -0.9) + _v4(5, 0.0))
    before = Path(db).read_bytes()
    out = tmp_path / "rep.md"
    assert script.main(["--db", db, "--date", "2026-12-20", "--out", str(out)]) == 0
    assert Path(db).read_bytes() == before  # nothing written
    text = out.read_text(encoding="utf-8")
    sec = text[text.index("## S12: правило зняття") :]
    assert "- вердикт: зупинити й розібрати виконання" in sec
    assert "- v9: n 30, днів" in sec and "середній R -0.900" in sec
    assert "- вхід v4 (v4 + v9): n 35," in sec and "рано (35 з 100), без вердикту" in sec
    assert "правило: n ≥ 30 і середній R < -0.827 → зупинити й розібрати виконання" in sec
    assert "правило: n ≥ 100 і середній R < -0.500 → зняти" in sec
    assert text.index("## Подробиці") < text.index("## S12: правило зняття")


def test_script_without_demo_table_still_prints_an_early_section(tmp_path):
    db = str(tmp_path / "empty.sqlite")
    sqlite3.connect(db).close()
    out = tmp_path / "rep.md"
    assert script.main(["--db", db, "--date", "2026-12-20", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "## S12: правило зняття" in text and "- вердикт: рано" in text
