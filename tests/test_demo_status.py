"""demo_status (item 409 of IDEA-20261102-standing-improvements): the demo block of scripts/checkpoint_report.py as a
read-only MCP tool. A small store with demo_trade rows (made up: closed, open, orphaned, pending, one P&L mismatch);
the tool's numbers must equal the report's on the same DB. No network."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import FakeEx, State, make_transports
from test_server import call
from tradedesk.analytics import checkpoint as cp
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store import Store
from tradedesk.tools import demo_status as demo_status_tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import checkpoint_report as script  # noqa: E402

H = 3_600_000
NOW_DT = datetime(2026, 11, 2, 12, tzinfo=UTC)
NOW = int(NOW_DT.timestamp() * 1000)
V9 = cp._ms(2026, 10, 31, 10)  # after S12's v9 epoch
N_OPEN = 12


def _row(lid: str, setup: str, status: str, opened: int | None, **kw) -> dict:
    base = {
        "link_id": lid, "venue": "bybit-demo", "symbol": f"{lid.upper()}/USDT:USDT", "side": "long", "setup": setup,
        "timeframe": "4h", "signal_ts": (opened or V9) - 8 * H, "entry": 1.0, "stop": 0.9, "target": 1.2, "qty": 100.0,
        "leverage": 5, "risk_usd": 100.0, "comment": "c", "status": status, "opened_ts": opened, "fill": 1.0 if opened else None,
    }  # fmt: skip
    return base | kw


def demo_rows() -> list[dict]:
    rows = []
    for i in range(31):  # S3 v3: 31 closed, mean -0.05 -> a verdict
        o = V9 - 48 * H + i * H
        rows.append(_row(f"s3-{i}", "S3", "closed", o, closed_ts=o + H, r_multiple=-0.05, version="v3"))
    for i in range(3):  # S12 v9: 3 closed -> «рано»
        o = V9 + i * H
        rows.append(_row(f"s12-{i}", "S12", "closed", o, closed_ts=o + H, r_multiple=0.4, version="v9", side="short"))
    # S14: one closed trade whose recorded P&L (-50) disagrees with the prices (+10 = (1.1 - 1.0) * 100, no fees)
    rows.append(_row("bad", "S14", "closed", V9, closed_ts=V9 + H, r_multiple=-0.5, version="v3", exit=1.1, pnl_usd=-50.0, fees_usd=0.0))
    for i in range(N_OPEN):  # S38 v5: open positions, one with its stop at break-even
        kw = {"be_ts": V9 + 2 * H, "be_stop": 1.001} if i == 0 else {}
        rows.append(_row(f"o{i}", "S38", "open", V9 + i * H, version="v5", side="short", **kw))
    rows.append(_row("orph", "S3", "orphaned", V9 + H, version="v3"))
    rows.append(_row("pend", "S28", "pending", None, version="v8", rest_until=NOW + H))
    return rows


async def seed(db: str, rows: list[dict] | None = None) -> None:
    st = Store(db)
    await st.init()
    for row in demo_rows() if rows is None else rows:
        await st.demo_trade_insert(row)
    await st.aclose()


def demo_rt(db_path: str) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=db_path),
        transports=make_transports(State()),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW_DT
    return rt


async def demo_envs(db_path: str, *args: dict) -> list[dict]:
    rt = demo_rt(db_path)
    try:
        srv, _ = build_server(rt)
        return [await call(srv, "demo_status", **a) for a in args]
    finally:
        await rt.aclose()


async def test_demo_status_matches_the_checkpoint_report_on_the_same_db(tmp_path):
    db = str(tmp_path / "demo.sqlite")
    await seed(db)
    default, full = await demo_envs(db, {}, {"include_details": True})
    assert default["ok"] and full["ok"] and not default["missing"], default["missing"]
    # the report on the same DB (its read-only loader, its build): the demo rows, notes and S12 rule
    con = script.connect_ro(db)
    rep = script.build_report({"demo_trades": script.load_demo(con)}, NOW)
    con.close()
    want = {r["key"]: r for r in rep["forwards"] if r["group"] == "demo"}
    got = {s["setup"]: s for s in full["data"]["setups"]}
    assert [s["setup"] for s in default["data"]["setups"]] == [s.setup for s in cp.DEMO]
    for spec in cp.DEMO:
        row, s = want[f"demo:{spec.setup}"], got[spec.setup]
        assert (s["n"], s["verdict"], s["side"], s["name"], s["notes"]) == (
            row["n"],
            row["verdict"],
            row["side"],
            row["name"],
            row["notes"],
        )
        assert s["mean_r"] == (None if row["metric"] is None else pytest.approx(row["metric"], abs=1e-4))
        assert s["band"]["n_ref"] == row["band"]["n_ref"]
        assert s["band"]["decision"] == pytest.approx(list(row["band"]["decision"]), abs=1e-9)
        if spec.fill_share is not None:
            fill = want[f"demo:{spec.setup}:fill"]
            assert (s["fill"]["n"], s["fill"]["verdict"], s["fill"]["passport"]) == (fill["n"], fill["verdict"], spec.fill_share)
        else:
            assert "fill" not in s
    assert full["data"]["notes"] == default["data"]["notes"] == rep["notes"]
    assert any(n.startswith("Угоди з розбіжним P&L: 1 — S14") for n in rep["notes"])
    assert any(n.startswith("демо-угоди orphaned") for n in rep["notes"])
    s12 = rep["s12_stop_rule"]
    assert default["data"]["s12_stop_rule"]["verdict"] == s12["verdict"] == cp.EARLY
    assert [c["n"] for c in default["data"]["s12_stop_rule"]["checks"]] == [c["n"] for c in s12["checks"]] == [3, 3]
    # S3 has a verdict on 31 closed trades; the orphan stays out of the sample
    s3 = got["S3"]
    assert (s3["n"], s3["verdict"], s3["mean_r"]) == (31, cp.OK, -0.05)
    assert s3["band"] == {"n_ref": 30, "decision": [-0.719, 0.652], "iid": [-0.473, 0.361], "rolling": [-0.719, 0.652]}


async def test_demo_status_open_positions_compact_and_full(tmp_path):
    db = str(tmp_path / "demo.sqlite")
    await seed(db)
    default, full = await demo_envs(db, {}, {"include_details": True})
    d, f = default["data"], full["data"]
    assert d["open_count"] == f["open_count"] == N_OPEN and d["open_by_setup"] == {"S38": N_OPEN}
    assert d["trades_by_status"] == {"closed": 35, "open": N_OPEN, "orphaned": 1, "pending": 1}
    # newest fills first; the compact list is the head of the full one, cut to the key fields
    assert [x["symbol"] for x in f["open"]] == [f"O{i}/USDT:USDT" for i in reversed(range(N_OPEN))]
    assert d["open"] == [{k: x[k] for k in d["open"][0]} for x in f["open"][:10]]
    assert any(f"the newest 10 of {N_OPEN} open positions" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    be = next(x for x in f["open"] if x["link_id"] == "o0")
    assert (be["stop"], be["initial_stop"], be["break_even_at"]) == (1.001, 0.9, "2026-10-31T12:00+00:00")
    # closed trades only with include_details: newest closes first
    assert "closed_recent" not in d and len(f["closed_recent"]) == 35
    assert f["closed_recent"][0]["link_id"] == "s12-2" and f["closed_recent"][0]["r_multiple"] == 0.4
    assert all("notes" not in s for s in d["setups"]) and all("notes" in s for s in f["setups"])
    assert any("NOT evidence of an edge" in w for w in default["warnings"])


async def test_demo_status_empty_store_answers_early_everywhere(tmp_path):
    db = str(tmp_path / "empty.sqlite")
    await seed(db, rows=[])
    (env,) = await demo_envs(db, {})
    assert env["ok"] and not env["missing"]
    d = env["data"]
    assert d["open_count"] == 0 and d["open"] == [] and d["trades_by_status"] == {} and d["notes"] == []
    assert all(s["n"] == 0 and s["mean_r"] is None and s["verdict"] == cp.EARLY for s in d["setups"])
    assert d["s12_stop_rule"]["verdict"] == cp.EARLY
    assert any("no demo trades in the store" in w for w in env["warnings"])


async def test_demo_status_store_off_or_failing_goes_to_missing(tmp_path):
    (off,) = await demo_envs("", {})
    assert off["ok"] is False and off["data"] is None
    assert [(m["field"], m["reason"]) for m in off["missing"]] == [("store", "config")]

    db = str(tmp_path / "demo.sqlite")
    await seed(db)
    rt = demo_rt(db)
    try:
        srv, _ = build_server(rt)
        await rt.store.init()

        async def broken(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        rt.store.demo_trade_list = broken
        env = await call(srv, "demo_status")
    finally:
        await rt.aclose()
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["reason"], m["detail"]) for m in env["missing"]] == [
        ("store", "backend_error", "OperationalError: disk I/O error")
    ]


async def test_demo_status_warns_when_the_store_read_fills_its_row_cap(tmp_path, monkeypatch):
    # review of item 409, finding 1: a read that returns the whole cap may have left the oldest trades out
    db = str(tmp_path / "demo.sqlite")
    await seed(db)
    n = len(demo_rows())
    monkeypatch.setattr(demo_status_tool, "MAX_ROWS", n + 1)
    (below,) = await demo_envs(db, {})
    monkeypatch.setattr(demo_status_tool, "MAX_ROWS", n)
    (at_cap,) = await demo_envs(db, {})
    assert below["ok"] and at_cap["ok"]
    assert not any("cap of" in w for w in below["warnings"])
    assert any(f"cap of {n} demo_trade rows" in w and "older trades may be left out" in w for w in at_cap["warnings"])


def test_demo_status_cuts_a_long_note_at_a_list_boundary(monkeypatch):
    # review of item 440, finding 1: the compact cut ends on a whole list item, not inside a trade id
    monkeypatch.setattr(demo_status_tool, "NOTE_CHARS", 30)
    cut = demo_status_tool._cut_note
    assert cut("short, note") == "short, note"
    assert cut("orphaned: 4 — S38 AAAUSDT, S38 BBBUSDT, S38 CCCUSDT") == "orphaned: 4 — S38 AAAUSDT …"
    assert cut("x, " + "y" * 40) == "x, " + "y" * 25 + " …"  # the only separator is too early: cut by characters
    assert cut("z" * 40) == "z" * 28 + " …"  # no separator: cut by characters
    assert all(len(cut(n)) <= 30 for n in ("x, " + "y" * 40, "z" * 40, "a, b; " + "c" * 40))


def test_demo_status_cut_note_keeps_the_explanatory_tail(monkeypatch):
    # review of item 442, findings 4 and 5: the tail after the list survives the cut, the list still ends on a whole item
    monkeypatch.setattr(demo_status_tool, "NOTE_CHARS", 60)
    cut = demo_status_tool._cut_note
    note = "orphaned: 5 — S38 AAAUSDT, S38 BBBUSDT, S38 CCCUSDT, S38 DDDUSDT, S38 EEEUSDT; check by hand"
    assert cut(note) == "orphaned: 5 — S38 AAAUSDT, S38 BBBUSDT …; check by hand"
    assert len(cut(note)) <= 60
    # a tail longer than a third of the limit is not kept: the note is cut as a plain list
    long_tail = "orphaned: 5 — S38 AAAUSDT, S38 BBBUSDT, S38 CCCUSDT; " + "t" * 25
    assert cut(long_tail) == "orphaned: 5 — S38 AAAUSDT, S38 BBBUSDT …"
    # a tail with no ', ' in the second half of the room left: the list is cut by characters, the tail kept
    no_sep = "orphaned: " + "Q" * 60 + "; check"
    # 41 = 60 (cap) - 7 (len "; check") - 2 (len " …") - 10 (len "orphaned: ")
    assert cut(no_sep) == "orphaned: " + "Q" * 41 + " …; check" and len(cut(no_sep)) == 60


def test_demo_status_cut_note_semicolon_inside_the_last_entry(monkeypatch):
    # review 2026-11-23-demo-status-note-cut, finding 6 (known limitation): a '; ' inside the last trade entry is taken for the explanation
    # separator, so the kept "tail" is a piece of that entry; the cut still fits the cap, ends on a whole item, has ' …'
    monkeypatch.setattr(demo_status_tool, "NOTE_CHARS", 60)
    cut = demo_status_tool._cut_note
    note = "orphaned: 4 — S38 AAAUSDT, S38 BBBUSDT, S38 CCCUSDT, S38 DDDUSDT (qty 0; stuck)"
    out = cut(note)
    assert out == "orphaned: 4 — S38 AAAUSDT, S38 BBBUSDT …; stuck)"
    assert len(out) <= 60 and " …" in out
    head = out.split(" …")[0]
    assert note.startswith(head) and note[len(head) : len(head) + 2] == ", "  # no trade id cut in half


async def test_demo_status_skips_a_corrupt_row_with_a_warning(tmp_path):
    db = str(tmp_path / "demo.sqlite")
    await seed(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE demo_trade SET r_multiple = 'abc' WHERE link_id = 's3-0'")
    con.commit()
    con.close()
    (env,) = await demo_envs(db, {})
    assert env["ok"]
    s3 = next(s for s in env["data"]["setups"] if s["setup"] == "S3")
    assert s3["n"] == 30  # the corrupt row is left out, the rest answers
    assert any("1 stored demo_trade rows skipped" in w for w in env["warnings"])
