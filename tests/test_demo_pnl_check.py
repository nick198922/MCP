"""Closed demo trades with a P&L that disagrees with their prices (item 140 of the standing card): the pure check and
the read-only checkpoint note."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from tradedesk.analytics import checkpoint as cp
from tradedesk.analytics import demo_pnl_check as pc
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import checkpoint_report as script  # noqa: E402

NOW = cp._ms(2026, 11, 6, 12)


def _row(lid: str, side: str = "long", *, fill=100.0, exit=110.0, qty=2.0, pnl=None, fees=0.2, **kw):
    """A closed row; ``pnl`` defaults to the price P&L (consistent)."""
    sign = 1 if side == "long" else -1
    if pnl is None:
        pnl = sign * (exit - fill) * qty - (fees or 0.0)
    row = {"link_id": lid, "setup": "S12", "symbol": f"{lid}/USDT:USDT", "side": side, "status": "closed", "entry": fill}
    return row | {"fill": fill, "exit": exit, "qty": qty, "pnl_usd": pnl, "fees_usd": fees} | kw


# POWR S12 of docs/verification/2026-11-02-demo-execution-quality.md (M5): +24.75 USD recorded, prices give about -87
POWR = _row("POWR", fill=0.0576, exit=0.05692, qty=117_238, pnl=24.75, fees=7.38)


def test_consistent_rows_pass_and_powr_is_flagged():
    assert pc.pnl_mismatches([_row("OK"), _row("NOFEE", fees=None)]) == []
    (t,) = pc.pnl_mismatches([_row("OK"), POWR])
    assert (t["id"], t["setup"], t["symbol"]) == ("POWR", "S12", "POWR/USDT:USDT")
    assert t["recorded"] == pytest.approx(24.75)
    assert t["expected"] == pytest.approx(117_238 * (0.05692 - 0.0576) - 7.38, rel=1e-9)  # about -87.1
    assert t["gap"] == pytest.approx(t["recorded"] - t["expected"], rel=1e-9)
    assert t["tol"] == pytest.approx(0.005 * 0.0576 * 117_238, rel=1e-9)  # 0.5 % of notional, above the $0.5 floor


def test_short_sign_and_tolerance_edges():
    short = _row("SH", "short", fill=100.0, exit=90.0, qty=2.0)  # +20 - fees
    assert pc.expected_pnl(short) == pytest.approx(19.8)
    assert pc.pnl_mismatches([short]) == []
    # the same numbers read as a long would be -20.2: flagged
    assert [t["id"] for t in pc.pnl_mismatches([_row("SH", "long", fill=100.0, exit=90.0, qty=2.0, pnl=19.8)])] == ["SH"]
    # notional 200: tolerance max(1.0, 0.5) = 1.0; 0.9 off passes, 1.1 off is flagged
    assert pc.pnl_mismatches([_row("A", pnl=19.8 + 0.9)]) == []
    assert len(pc.pnl_mismatches([_row("A", pnl=19.8 - 1.1)])) == 1
    # a small trade (notional 20): the $0.5 floor applies
    small = dict(fill=10.0, exit=11.0, qty=2.0, fees=0.0)
    assert pc.pnl_mismatches([_row("S", pnl=2.4, **small)]) == []
    assert pc.pnl_mismatches([_row("S", pnl=2.6, **small)])[0]["tol"] == pytest.approx(0.5)
    # without a fill the planned entry is used
    assert pc.expected_pnl(_row("E", entry=100.0, pnl=19.8) | {"fill": None}) == pytest.approx(19.8)


def test_missing_fields_and_other_statuses_are_skipped():
    rows = [
        _row("NANPNL", pnl=float("nan")),
        _row("NOPNL", pnl_usd=None),
        _row("NOEXIT", exit=None, pnl=1.0),
        _row("NOQTY", qty=None, pnl=1.0),
        _row("NOENTRY", fill=None, entry=None, pnl=1.0),
        _row("SIDE", side="flat", pnl=1000.0),
        _row("TEXT", exit="1.0", pnl=1.0),
    ]
    assert pc.pnl_mismatches(rows) == []
    assert pc.unchecked(rows) == len(rows)
    open_bad = _row("OPEN", pnl=1000.0, status="open")
    assert pc.pnl_mismatches([open_bad]) == [] and pc.unchecked([open_bad]) == 0
    assert pc.unchecked([_row("OK"), POWR]) == 0


def test_checkpoint_note_only_when_some():
    notes = cp.build({"demo_trades": [_row("OK"), POWR]}, NOW)["notes"]
    (note,) = [x for x in notes if x.startswith("Угоди з розбіжним P&L")]
    assert note.startswith("Угоди з розбіжним P&L: 1 — S12 POWR/USDT:USDT (+24.75 проти -87.")
    assert not any("розбіжним P&L" in x for x in cp.build({"demo_trades": [_row("OK")]}, NOW)["notes"])
    assert not any("розбіжним P&L" in x for x in cp.build({}, NOW)["notes"])


# ------------------------------------------------------------------------------------------------------ script


def _db(tmp_path: Path, rows: list[dict]) -> str:
    db = str(tmp_path / "fixture.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    asyncio.run(go())
    con = sqlite3.connect(db)
    cols = (
        "link_id, venue, symbol, side, setup, timeframe, signal_ts, entry, stop, target, qty, leverage, risk_usd, comment,"
        " status, fill, opened_ts, exit, closed_ts, pnl_usd, fees_usd"
    )
    for r in rows:
        con.execute(
            f"INSERT INTO demo_trade ({cols}) VALUES ({', '.join('?' * 21)})",
            (r["link_id"], "bybit-demo", r["symbol"], r["side"], r["setup"], "4h", NOW - 30 * 3_600_000, r["entry"], 1, 2, r["qty"], 5, 100, "c", r["status"], r["fill"], NOW - 20 * 3_600_000, r["exit"], NOW - 3_600_000, r["pnl_usd"], r["fees_usd"]),
        )  # fmt: skip
    con.commit()
    con.close()
    return db


def test_script_prints_the_line_read_only(tmp_path, capsys):
    db = _db(tmp_path, [_row("OK"), POWR])
    before = Path(db).read_bytes()
    assert script.main(["--db", db, "--date", "2026-11-06"]) == 0
    assert "Примітка: Угоди з розбіжним P&L: 1 — S12 POWR/USDT:USDT" in capsys.readouterr().out
    assert Path(db).read_bytes() == before  # nothing written
    clean = tmp_path / "clean"
    clean.mkdir()
    assert script.main(["--db", _db(clean, [_row("OK")]), "--date", "2026-11-06"]) == 0
    assert "розбіжним P&L" not in capsys.readouterr().out
